from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.handlers.session_handler as session_handler_module
from config.v2_compat import to_app_config
from config.v2_config import AgentsConfig, ClaudeConfig, RuntimeConfig, SlackConfig, V2Config
from config.v2_settings import RoutingSettings
from core import git_runtime as git_runtime_module
from core.handlers.session_handler import SessionHandler
from core.runtime_activation import RuntimeActivationRegistry
from core.runtime_ownership import SessionRuntimeDisposition
from modules.claude_sdk_compat import CLAUDE_SDK_MAX_BUFFER_SIZE
from modules.im import MessageContext


@dataclass
class _ClaudeRuntimeConfig:
    permission_mode: str = "bypassPermissions"
    cwd: str = "/tmp/workdir"
    system_prompt: str | None = None
    cli_path: str | None = "/usr/local/bin/claude-proxy"


@dataclass
class _Config:
    platform: str = "slack"
    reply_enhancements: bool = False
    claude: _ClaudeRuntimeConfig = field(default_factory=_ClaudeRuntimeConfig)


class _Sessions:
    @staticmethod
    def get_claude_session_id(settings_key, base_session_id):
        assert settings_key == "test::C123"
        assert base_session_id == "slack_C123"
        return None

    @staticmethod
    def get_agent_session_id(settings_key, base_session_id, agent_name):
        return None

    @staticmethod
    def ensure_agent_session_id(settings_key, agent_name, base_session_id, **_kwargs):
        return "sesk8m4q2p7x"


class _SettingsManager:
    def __init__(self) -> None:
        self.sessions = _Sessions()

    @staticmethod
    def get_channel_settings(settings_key):
        assert settings_key == "test::C123"
        return None

    @staticmethod
    def get_channel_routing(settings_key):
        return None


class _Controller:
    def __init__(self, working_path: Path) -> None:
        self.config = _Config()
        self.im_client = type("IM", (), {"formatter": None})()
        self.settings_manager = _SettingsManager()
        self.platform_settings_managers = {"slack": self.settings_manager}
        self.session_manager = object()
        self.claude_sessions = {}
        self.receiver_tasks = {}
        self.stored_session_mappings = {}
        self._working_path = working_path
        def ownership(target):
            return SimpleNamespace(
                resource_key=target.resource_key,
                disposition=SessionRuntimeDisposition.RECLAIMABLE,
                blocks_reclamation=False,
                needs_session_delivery_wake=False,
                needs_request_wake=False,
            )

        self.runtime_ownership = SimpleNamespace(
            snapshot=ownership,
            snapshot_many=lambda targets: tuple(
                ownership(target) for target in targets
            ),
        )

    def get_cwd(self, context) -> str:
        return str(self._working_path)

    @staticmethod
    def _get_settings_key(context) -> str:
        return context.channel_id

    @staticmethod
    def _get_session_key(context) -> str:
        return f"{getattr(context, 'platform', None) or 'test'}::{context.channel_id}"

    def get_settings_manager_for_context(self, context=None):
        return self.settings_manager


def _run_session(handler: SessionHandler, context: MessageContext):
    return asyncio.run(handler.get_or_create_claude_session(context))


class _StubClaudeAgentOptions:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)
        if not hasattr(self, "cli_path"):
            self.cli_path = None
        self.continue_conversation = False


def _disconnect_counting_client(captured: dict[str, Any]):
    """Stub ClaudeSDKClient that records how many times it was disconnected."""

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["disconnects"] = 0

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            captured["disconnects"] += 1

    return _StubClaudeSDKClient


def test_to_app_config_preserves_claude_cli_path() -> None:
    v2 = V2Config(
        mode="self_host",
        version="2",
        slack=SlackConfig(),
        runtime=RuntimeConfig(default_cwd="/tmp/workdir"),
        agents=AgentsConfig(claude=ClaudeConfig(cli_path="/usr/local/bin/claude-proxy")),
    )

    compat = to_app_config(v2)

    assert compat.claude.cli_path == "/usr/local/bin/claude-proxy"


def test_session_handler_passes_configured_claude_cli_path(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    client = _run_session(handler, context)

    assert captured["connected"] is True
    assert captured["options"].cli_path == "/usr/local/bin/claude-proxy"
    assert captured["options"].max_buffer_size == CLAUDE_SDK_MAX_BUFFER_SIZE
    assert captured["options"].skills == []
    assert captured["options"].env["AVIBE_SKILL_WORKING_DIR"] == str(tmp_path.resolve())
    assert controller.claude_sessions[f"slack_C123:{tmp_path}"] is client
    assert getattr(client, "_vibe_runtime_base_session_id") == "slack_C123"
    assert getattr(client, "_vibe_runtime_session_key") == f"slack_C123:{tmp_path}"


def test_session_handler_uses_native_cli_launch_reasoning_catalog(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from modules.agents.model_hub import ModelHubLaunch

    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            return None

    class _Runtime:
        async def resolve(self, backend, requested_model, **_kwargs):
            return ModelHubLaunch(
                backend=backend,
                channel="native_cli",
                requested_model=requested_model,
                target_model=requested_model,
                runtime_model=requested_model,
                source_id="src_native01",
                context_window=128_000,
                max_output_tokens=32_000,
                reasoning_efforts=("max",),
            )

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(
        "vibe.backend_model_catalog.catalog_reasoning_efforts_for_model",
        lambda *_args: ["low"],
    )

    controller = _Controller(tmp_path)
    controller.model_hub_runtime = _Runtime()
    controller.settings_manager.get_channel_routing = lambda _key: RoutingSettings(
        model="claude-opus-4-6",
        reasoning_effort="max",
    )
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    assert captured["options"].effort == "max"
    assert captured["options"].env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "128000"
    assert captured["options"].env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32000"


def test_session_handler_pins_hub_connection_in_launch_settings(monkeypatch, tmp_path: Path) -> None:
    import json
    from modules.agents.model_hub import ModelHubLaunch

    captured = {}

    class Client:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self):
            pass

    class Runtime:
        async def resolve(self, backend, requested_model, **kwargs):
            return ModelHubLaunch(
                backend=backend, channel="hub", requested_model=requested_model,
                target_model="gpt-6-astra", runtime_model=requested_model,
                source_id="src_hubsettings", gateway_base_url="http://127.0.0.1:18443/claude",
                gateway_token="hub-settings-fixture-token",
            )

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", Client)
    controller = _Controller(tmp_path)
    controller.model_hub_runtime = Runtime()
    controller.settings_manager.get_channel_routing = lambda _: RoutingSettings(model="claude-opus-5")
    _run_session(SessionHandler(controller), MessageContext(user_id="U123", channel_id="C123"))

    options = captured["options"]
    settings = json.loads(options.settings)
    assert settings["env"]["ANTHROPIC_BASE_URL"] == options.env["ANTHROPIC_BASE_URL"]
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == "hub-settings-fixture-token"
    assert settings["env"]["ANTHROPIC_API_KEY"] == ""
    assert settings["autoMemoryEnabled"] is False
    assert options.setting_sources == ["project", "local"]
    assert options.extra_args["model"] == "claude-opus-5"


def test_session_handler_recreates_cached_claude_client_when_catalog_limits_change(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from modules.agents.model_hub import ModelHubLaunch

    captured: dict[str, Any] = {"clients": []}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.disconnects = 0
            captured["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    class _Runtime:
        def __init__(self) -> None:
            self.context_windows = iter((128_000, 256_000))

        async def resolve(self, backend, requested_model, **_kwargs):
            context_window = next(self.context_windows)
            return ModelHubLaunch(
                backend=backend,
                channel="native_cli",
                requested_model=requested_model,
                target_model=requested_model,
                runtime_model=requested_model,
                source_id="src_native01",
                context_window=context_window,
                max_output_tokens=32_000,
                reasoning_efforts=("high",),
            )

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.model_hub_runtime = _Runtime()
    controller.settings_manager.get_channel_routing = lambda _key: RoutingSettings(
        model="claude-opus-4-6",
        reasoning_effort="high",
    )
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    first_client = _run_session(handler, context)
    second_client = _run_session(handler, context)

    assert first_client is not second_client
    assert first_client.disconnects == 1
    assert len(captured["clients"]) == 2
    assert first_client.options.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "128000"
    assert second_client.options.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "256000"


def test_claude_system_prompt_follows_live_memory_enabled_state(tmp_path: Path) -> None:
    controller = _Controller(tmp_path)
    controller.config.memory = type("MemoryConfig", (), {"enabled": False})()
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform="avibe",
        platform_specific={"memory_cli_admitted": True},
    )

    disabled = asyncio.run(
        handler._build_claude_system_prompt(
            context,
            session_key="test::C123",
            agent_name="claude",
            session_anchor="slack_C123",
            agent_system_prompt=None,
        )
    )
    controller.config.memory.enabled = True
    enabled = asyncio.run(
        handler._build_claude_system_prompt(
            context,
            session_key="test::C123",
            agent_name="claude",
            session_anchor="slack_C123",
            agent_system_prompt=None,
        )
    )

    assert "## Personal Memory" not in disabled["append"]
    assert "## Personal Memory" in enabled["append"]
    assert 'vibe memory search "<query>" --json' in enabled["append"]


def test_session_handler_injects_vendored_git_into_gitless_child_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            return None

    def inject_git(env, *, base_env, working_dir):
        assert "PATH" not in env
        assert base_env is session_handler_module.os.environ
        assert working_dir == str(tmp_path)
        env["PATH"] = "/managed/git/bin"
        return True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(git_runtime_module, "prepend_vendored_git_to_path", inject_git)

    _run_session(
        SessionHandler(_Controller(tmp_path)),
        MessageContext(user_id="U123", channel_id="C123"),
    )

    assert captured["options"].env["PATH"] == "/managed/git/bin"


def test_session_handler_moves_claude_process_into_agent_cgroup(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[int, str]] = []

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self._transport = type("Transport", (), {"_process": type("Process", (), {"pid": 9753})()})()

        async def connect(self) -> None:
            return None

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)

    monkeypatch.setattr(
        session_handler_module,
        "governor_from_controller",
        lambda _controller: type(
            "Governor",
            (),
            {"apply_to_pid": lambda self, pid, label="agent": calls.append((pid, label)) or True},
        )(),
    )

    _run_session(handler, MessageContext(user_id="U123", channel_id="C123"))

    assert calls == [(9753, "claude")]


def test_session_handler_keeps_sdk_default_for_default_claude_binary(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.config.claude.cli_path = "claude"
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    assert captured["connected"] is True
    assert captured["options"].cli_path is None


def test_session_handler_sets_claude_fork_session_for_pending_native_fork(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    class _ForkSessions(_Sessions):
        @staticmethod
        def get_claude_session_id(settings_key, base_session_id):
            return None

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.settings_manager.sessions = _ForkSessions()
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="ses-target",
        platform="avibe",
        platform_specific={
            "agent_session_target": {
                "id": "ses-target",
                "agent_backend": "claude",
                "native_session_id": "",
                "model": "claude-sonnet-4-5",
                "reasoning_effort": "high",
                "native_session_fork": {
                    "source_session_id": "ses-source",
                    "source_native_session_id": "claude-source",
                    "source_backend": "claude",
                },
            }
        },
    )

    client = _run_session(handler, context)

    assert captured["connected"] is True
    assert captured["options"].resume == "claude-source"
    assert captured["options"].fork_session is True
    assert captured["options"].extra_args == {
        "replay-user-messages": None,
        "model": "claude-sonnet-4-5",
    }
    assert captured["options"].settings == '{"autoMemoryEnabled":false}'
    assert captured["options"].effort == "high"
    assert not hasattr(client, "_vibe_native_session_id")
    prompt_value = captured["options"].system_prompt
    prompt = prompt_value["append"] if isinstance(prompt_value, dict) else prompt_value
    assert "Current session id: `ses-target`" in prompt
    assert "This Agent Session was forked from `ses-source`." in prompt
    assert "The authoritative Avibe session id for this fork is `ses-target`." in prompt
    assert "treat it as historical source-context only" in prompt
    assert "use `ses-target` for Show Pages" not in prompt


def test_session_handler_disallows_remote_unsafe_claude_tools(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    assert captured["connected"] is True
    expected = ["AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "Skill"]
    if not session_handler_module.CLAUDE_SDK_HOOKS_AVAILABLE or session_handler_module.HookMatcher is None:
        expected.append("Workflow")
    assert captured["options"].disallowed_tools == expected


def test_session_handler_ensures_agent_session_id_before_prompt(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class _PromptSessions(_Sessions):
        @staticmethod
        def ensure_agent_session_id(settings_key, agent_name, base_session_id, **_kwargs):
            assert settings_key == "test::C123"
            assert agent_name == "claude"
            assert base_session_id == "slack_C123"
            return "sesk8m4q2p7x"

        @staticmethod
        def get_claude_session_id(settings_key, base_session_id):
            assert settings_key == "test::C123"
            assert base_session_id == "slack_C123"
            return None

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.settings_manager.sessions = _PromptSessions()
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    prompt_value = captured["options"].system_prompt
    prompt = prompt_value["append"] if isinstance(prompt_value, dict) else prompt_value
    assert captured["connected"] is True
    assert "Current session id: `sesk8m4q2p7x`" in prompt
    assert "load the `use-show-pages` Skill" in prompt
    assert "- use-show-pages:" in prompt
    assert "`vibe show path`" not in prompt
    assert "--session-id sesk8m4q2p7x" not in prompt
    assert "--session-key" not in prompt


def test_session_handler_preserves_passed_agent_system_prompt(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _PromptSessions(_Sessions):
        @staticmethod
        def ensure_agent_session_id(settings_key, agent_name, base_session_id, **_kwargs):
            return "sesk8m4q2p7x"

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.settings_manager.sessions = _PromptSessions()
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    asyncio.run(
        handler.get_or_create_claude_session(
            context,
            agent_system_prompt="Use the release-reviewer Vibe Agent policy.",
        )
    )

    prompt_value = captured["options"].system_prompt
    prompt = prompt_value["append"] if isinstance(prompt_value, dict) else prompt_value
    assert captured["connected"] is True
    assert "Use the release-reviewer Vibe Agent policy." in prompt


def test_session_handler_omits_show_pages_prompt_when_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.config.show_pages_prompt = False
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    prompt_value = captured["options"].system_prompt
    prompt = prompt_value["append"] if isinstance(prompt_value, dict) else prompt_value
    assert captured["connected"] is True
    assert "# Avibe" in prompt
    assert "Current session id: `sesk8m4q2p7x`" in prompt
    assert "## Show Pages" not in prompt
    assert "vibe show path" not in prompt


def test_session_handler_recreates_cached_claude_client_when_prompt_changes(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {"clients": []}

    class _PromptSessions(_Sessions):
        current_id = "sesold"

        @classmethod
        def ensure_agent_session_id(cls, settings_key, agent_name, base_session_id, **_kwargs):
            assert settings_key == "test::C123"
            assert agent_name == "claude"
            assert base_session_id == "slack_C123"
            return cls.current_id

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.disconnects = 0
            captured["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.settings_manager.sessions = _PromptSessions()
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    composite_key = f"slack_C123:{tmp_path}"

    first_client = _run_session(handler, context)
    _PromptSessions.current_id = "sesnew"
    second_client = _run_session(handler, context)

    assert first_client is not second_client
    assert first_client.disconnects == 1
    assert controller.claude_sessions[composite_key] is second_client
    assert len(captured["clients"]) == 2
    assert "Current session id: `sesold`" in first_client.options.system_prompt["append"]
    assert "Current session id: `sesnew`" in second_client.options.system_prompt["append"]


def test_session_handler_reuses_cached_claude_client_when_system_prompt_is_unchanged(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {"clients": []}

    class _PromptSessions(_Sessions):
        @staticmethod
        def ensure_agent_session_id(settings_key, agent_name, base_session_id, **_kwargs):
            assert settings_key == "slack::C123"
            assert agent_name == "claude"
            assert base_session_id == "slack_C123"
            return "sesk8m4q2p7x"

        @staticmethod
        def get_claude_session_id(settings_key, base_session_id):
            assert settings_key == "slack::C123"
            assert base_session_id == "slack_C123"
            return None

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.disconnects = 0
            captured["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

        async def set_model(self, model: str | None) -> None:
            self.model = model

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.settings_manager.sessions = _PromptSessions()
    handler = SessionHandler(controller)
    first_context = MessageContext(user_id="U123", channel_id="C123", platform="slack")
    second_context = MessageContext(user_id="U456", channel_id="C123", platform="slack")
    composite_key = f"slack_C123:{tmp_path}"

    first_client = _run_session(handler, first_context)
    second_client = _run_session(handler, second_context)

    assert first_client is second_client
    assert first_client.disconnects == 0
    assert len(captured["clients"]) == 1
    assert controller.claude_sessions[composite_key] is first_client
    assert "Use the current platform `slack`" in first_client.options.system_prompt["append"]
    assert "`slack/<user_id>`" in first_client.options.system_prompt["append"]
    assert "slack/U123" not in first_client.options.system_prompt["append"]
    assert "slack/U456" not in first_client.options.system_prompt["append"]


def test_session_handler_recreates_cached_claude_client_when_skill_bindings_change(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {"clients": []}
    skill_env = {"AVIBE_SKILL_WORKING_DIR": str(tmp_path)}

    class _PromptSessions(_Sessions):
        @staticmethod
        def ensure_agent_session_id(settings_key, agent_name, base_session_id, **_kwargs):
            return "sesk8m4q2p7x"

        @staticmethod
        def get_claude_session_id(settings_key, base_session_id):
            return None

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.disconnects = 0
            captured["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(
        session_handler_module,
        "managed_skill_environment",
        lambda _working_path, **_kwargs: dict(skill_env),
    )

    controller = _Controller(tmp_path)
    controller.settings_manager.sessions = _PromptSessions()
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    first_client = _run_session(handler, context)
    skill_env["AVIBE_BUILTIN_SKILLS_SNAPSHOT_ID"] = "a" * 64
    second_client = _run_session(handler, context)

    assert first_client is not second_client
    assert first_client.disconnects == 1
    assert len(captured["clients"]) == 2
    assert "AVIBE_BUILTIN_SKILLS_SNAPSHOT_ID" not in first_client.options.env
    assert second_client.options.env["AVIBE_BUILTIN_SKILLS_SNAPSHOT_ID"] == "a" * 64


def test_session_handler_recreates_terminated_cached_client_before_dispatch(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    captured: dict[str, Any] = {"clients": []}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.disconnects = 0
            self._transport = SimpleNamespace(_process=SimpleNamespace(returncode=None))
            captured["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    idle_wait = AsyncMock()
    handler._wait_for_claude_session_idle = idle_wait
    context = MessageContext(user_id="U123", channel_id="C123")
    composite_key = f"slack_C123:{tmp_path}"

    first_client = _run_session(handler, context)
    first_client.options.stderr("sandbox warning")
    first_client._transport._process.returncode = -6
    first_client._vibe_stderr_lines.extend(["fatal: Claude CLI aborted", "transport closed"])
    second_client = _run_session(handler, context)

    assert first_client is not second_client
    assert first_client.disconnects == 1
    assert controller.claude_sessions[composite_key] is second_client
    assert len(captured["clients"]) == 2
    idle_wait.assert_awaited_once_with(composite_key)
    assert "SIGABRT (signal 6)" in caplog.text
    assert "Claude CLI stderr for slack_C123:" in caplog.text
    assert "Claude stderr tail:\nsandbox warning\nfatal: Claude CLI aborted\ntransport closed" in caplog.text


def test_session_handler_waits_for_receiver_cleanup_outside_generation_lock(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {"clients": []}

    async def exercise() -> None:
        controller = _Controller(tmp_path)
        handler = SessionHandler(controller)
        composite_key = f"slack_C123:{tmp_path}"
        release_cleanup = asyncio.Event()

        class _StubClaudeSDKClient:
            def __init__(self, options):
                self.options = options
                self.disconnects = 0
                self._transport = SimpleNamespace(_process=SimpleNamespace(returncode=None))
                captured["clients"].append(self)

            async def connect(self) -> None:
                return None

            async def disconnect(self) -> None:
                self.disconnects += 1

        monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
        monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
        context = MessageContext(user_id="U123", channel_id="C123")
        first_client = await handler.get_or_create_claude_session(context)
        first_client._transport._process.returncode = -6

        async def receiver() -> None:
            await release_cleanup.wait()
            await handler.cleanup_session(
                composite_key,
                current_receiver_task=asyncio.current_task(),
            )

        receiver_task = asyncio.create_task(receiver())
        handler.receiver_tasks[composite_key] = receiver_task

        eviction = asyncio.create_task(
            handler.get_or_create_claude_session(context)
        )
        await asyncio.sleep(0)
        assert not eviction.done()
        release_cleanup.set()
        second_client = await asyncio.wait_for(eviction, timeout=1)
        assert second_client is not first_client
        assert first_client.disconnects == 1
        assert len(captured["clients"]) == 2
        await receiver_task

    asyncio.run(exercise())


def test_session_handler_retires_model_hub_scope_for_dead_cached_client(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        controller = _Controller(tmp_path)
        handler = SessionHandler(controller)
        composite_key = f"slack_C123:{tmp_path}"
        client = SimpleNamespace(
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=1)),
            _vibe_model_hub_fingerprint="hub:http://127.0.0.1:18443:token",
        )
        handler.claude_sessions[composite_key] = client
        handler._wait_for_claude_receiver_cleanup = AsyncMock()
        handler._wait_for_claude_session_idle = AsyncMock()
        handler._cleanup_session_locked = AsyncMock()

        assert await handler._evict_terminated_cached_claude_session(composite_key, client)
        handler._cleanup_session_locked.assert_awaited_once_with(
            composite_key,
            retire_model_hub_scope=True,
            reason="cached_process_terminated",
        )

    asyncio.run(exercise())


def test_session_handler_marks_claude_sdk_session_process_owner(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    registered: list[dict[str, Any]] = []

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options
            self._transport = type("T", (), {"_process": type("P", (), {"pid": 4321})()})()

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(
        session_handler_module,
        "register_claude_owned_process",
        lambda client, **kwargs: registered.append({"client": client, **kwargs}),
    )

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    client = _run_session(handler, context)

    assert captured["connected"] is True
    assert captured["options"].env["AVIBE_CLAUDE_PROCESS_OWNER"] == "session"
    assert registered == [{"client": client, "native_session_id": None, "owner": "session"}]


def test_session_handler_injects_caller_context_env(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform_specific={
            "task_execution_id": "run-parent",
            "task_trigger_kind": "agent_run",
            "agent_session_target": {
                "id": "ses-parent",
                "agent_backend": "claude",
                "native_session_id": "claude-native",
            },
        },
    )

    _run_session(handler, context)

    env = captured["options"].env
    assert env["AVIBE_SESSION_ID"] == "ses-parent"
    assert env["AVIBE_RUN_ID"] == "run-parent"
    assert env["AVIBE_CALLER_SOURCE"] == "agent_run"
    assert env["AVIBE_CALLER_BACKEND"] == "claude"
    assert env["AVIBE_NATIVE_SESSION_ID"] == "claude-native"


def test_session_handler_injects_only_session_stable_creation_origin(
    monkeypatch, tmp_path: Path
) -> None:
    """The creation origin a Claude session may carry, and the two ids it may not.

    Round 14 gate item 3 (review comment 5121007240): a Harness definition created by
    ``vibe task add`` inside an Agent turn has to record the conversation it came from,
    and this env is the only hop those ids can travel. But a Claude SDK client is spawned
    ONCE per session with a fixed environment, and that same environment is what
    ``_reuse_cached_claude_session_if_available`` compares to decide whether the cached
    client is still valid. So:

    * the session-owned ids (platform, channel, session key, workspace) are baked in —
      that is what makes the notice able to name the conversation and rung (3) able to
      address it;
    * ``AVIBE_CALLER_USER_ID`` and ``AVIBE_CALLER_MESSAGE_ID`` are NOT. The message id
      changes every turn, so including it would respawn Claude on every message; the
      author changes per speaker in a shared channel, and
      ``test_session_handler_reuses_cached_claude_client_when_system_prompt_is_unchanged``
      pins that a channel session is shared across participants — so a baked-in author
      would later attribute another participant's definition to them and DM the wrong
      person.

    The visible cost is a Claude-created definition having no deep link (every permalink
    grammar needs the message id). Codex and OpenCode rewrite their caller env per turn
    and keep the full origin; that asymmetry is the unit-level
    ``test_a_session_scoped_caller_env_drops_only_the_per_turn_origin``.
    """

    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    # Keep the base session anchor at ``slack_C123`` (this file's session stub asserts
    # it): without this the message id would become the anchor, which is a different
    # decision from the one under test.
    controller.im_client.should_use_message_id_for_channel_session = lambda _context=None: False
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        message_id="1710000000.000200",
        platform_specific={
            "team_id": "T0999",
            "is_dm": False,
            "agent_session_target": {
                "id": "ses-parent",
                "agent_backend": "claude",
                "native_session_id": "claude-native",
            },
        },
    )

    _run_session(handler, context)

    env = captured["options"].env
    # The Slack adapter sets neither ``context.platform`` nor a payload ``platform``, so
    # the handler's own resolver is what supplies it — captured here as well, because a
    # missing platform would make the whole origin unnameable.
    assert env["AVIBE_CALLER_PLATFORM"] == "slack"
    assert env["AVIBE_CALLER_CHANNEL_ID"] == "C123"
    assert env["AVIBE_CALLER_SESSION_KEY"] == "slack::channel::C123"
    assert env["AVIBE_CALLER_WORKSPACE_ID"] == "T0999"

    assert "AVIBE_CALLER_USER_ID" not in env, (
        "a shared channel session must not bake in whichever participant spoke first"
    )
    assert "AVIBE_CALLER_MESSAGE_ID" not in env, (
        "a per-message id here would respawn the Claude client on every turn"
    )


def test_session_handler_coalesces_concurrent_claude_client_creates(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {"clients": [], "connects": 0}

    async def _run() -> None:
        connect_started = asyncio.Event()
        release_connect = asyncio.Event()

        class _StubClaudeSDKClient:
            def __init__(self, options):
                self.options = options
                captured["clients"].append(self)

            async def connect(self) -> None:
                captured["connects"] += 1
                connect_started.set()
                await release_connect.wait()

            async def disconnect(self) -> None:
                return None

            async def set_model(self, model: str | None) -> None:
                self.model = model

        monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
        monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

        controller = _Controller(tmp_path)
        handler = SessionHandler(controller)
        first_context = MessageContext(user_id="U123", channel_id="C123")
        second_context = MessageContext(user_id="U456", channel_id="C123")

        first = asyncio.create_task(handler.get_or_create_claude_session(first_context))
        await connect_started.wait()
        second = asyncio.create_task(handler.get_or_create_claude_session(second_context))
        await asyncio.sleep(0)

        assert len(captured["clients"]) == 1
        assert captured["connects"] == 1

        release_connect.set()
        first_client, second_client = await asyncio.gather(first, second)

        composite_key = f"slack_C123:{tmp_path}"
        assert first_client is second_client
        assert controller.claude_sessions[composite_key] is first_client
        assert len(captured["clients"]) == 1
        assert captured["connects"] == 1
        assert handler.claude_session_creates == {}

    asyncio.run(_run())


def test_session_handler_retries_waiting_claude_create_after_cancellation(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {"clients": [], "connects": 0}

    async def _run() -> None:
        connect_started = asyncio.Event()
        retry_connected = asyncio.Event()

        class _StubClaudeSDKClient:
            def __init__(self, options):
                self.options = options
                captured["clients"].append(self)

            async def connect(self) -> None:
                captured["connects"] += 1
                if captured["connects"] == 1:
                    connect_started.set()
                    await asyncio.Event().wait()
                else:
                    retry_connected.set()

            async def disconnect(self) -> None:
                return None

            async def set_model(self, model: str | None) -> None:
                self.model = model

        monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
        monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

        controller = _Controller(tmp_path)
        handler = SessionHandler(controller)
        first_context = MessageContext(user_id="U123", channel_id="C123")
        second_context = MessageContext(user_id="U456", channel_id="C123")

        first = asyncio.create_task(handler.get_or_create_claude_session(first_context))
        await connect_started.wait()
        second = asyncio.create_task(handler.get_or_create_claude_session(second_context))
        await asyncio.sleep(0)

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second_client = await asyncio.wait_for(second, timeout=1)

        composite_key = f"slack_C123:{tmp_path}"
        assert retry_connected.is_set()
        assert captured["connects"] == 2
        assert captured["clients"][-1] is second_client
        assert controller.claude_sessions[composite_key] is second_client
        assert handler.claude_session_creates == {}

    asyncio.run(_run())


def test_session_handler_does_not_resume_main_native_session_for_new_routing_subagent(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {"clients": []}
    from modules.agents import model_hub as model_hub_module

    original_resolve = model_hub_module.resolve_model_hub_launch

    async def capture_model_hub_scope(*args, **kwargs):
        captured["model_hub_process_scope"] = kwargs.get("process_scope")
        return await original_resolve(*args, **kwargs)

    class _SubagentSessions(_Sessions):
        @staticmethod
        def get_claude_session_id(settings_key, base_session_id):
            assert settings_key == "test::C123"
            assert base_session_id == "slack_C123"
            return "main-native-session"

        @staticmethod
        def get_agent_session_id(settings_key, base_session_id, agent_name):
            assert settings_key == "test::C123"
            assert base_session_id == "slack_C123:reviewer"
            assert agent_name == "claude"
            return None

    class _RoutingSettingsManager(_SettingsManager):
        def __init__(self) -> None:
            super().__init__()
            self.sessions = _SubagentSessions()

        @staticmethod
        def get_channel_routing(settings_key):
            assert settings_key == "C123"
            return type("Routing", (), {"claude_agent": "reviewer", "model": None, "reasoning_effort": None})()

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            captured["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(
        model_hub_module,
        "resolve_model_hub_launch",
        capture_model_hub_scope,
    )

    controller = _Controller(tmp_path)
    controller.settings_manager = _RoutingSettingsManager()
    controller.platform_settings_managers = {"slack": controller.settings_manager}
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform_specific={"routing_subagent": "reviewer"},
    )

    client = _run_session(handler, context)

    composite_key = f"slack_C123:reviewer:{tmp_path}"
    assert captured["model_hub_process_scope"] == composite_key
    assert client.options.resume is None
    assert not hasattr(client, "_vibe_native_session_id")
    assert controller.claude_sessions[composite_key] is client


def test_session_handler_forces_bypass_mode_and_auto_approves_claude_tool_permissions(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.config.claude.permission_mode = "default"
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)
    result = asyncio.run(captured["options"].can_use_tool("Bash", {"command": "git status"}, object()))

    assert captured["connected"] is True
    assert captured["options"].permission_mode == "bypassPermissions"
    assert captured["options"].sandbox == {"enabled": False}
    assert result.behavior == "allow"


def test_session_handler_auto_approves_all_claude_tool_permission_requests(
    monkeypatch, tmp_path: Path
) -> None:
    handler = SessionHandler(_Controller(tmp_path))

    result = asyncio.run(handler._allow_claude_bypass_tool("Bash", {"command": "git status"}, object()))

    assert result.behavior == "allow"


def test_session_handler_does_not_repeat_claude_model_control_request(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {"clients": []}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options
            captured["clients"].append(self)
            self.model_calls = []

        async def connect(self) -> None:
            captured["connected"] = True

        async def set_model(self, model) -> None:
            self.model_calls.append(model)

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform_specific={"agent_session_target": {"model": "claude-sonnet-4-5"}},
    )

    first_client = _run_session(handler, context)
    second_client = _run_session(handler, context)

    assert first_client is second_client
    assert len(captured["clients"]) == 1
    assert captured["options"].extra_args == {
        "replay-user-messages": None,
        "model": "claude-sonnet-4-5",
    }
    assert first_client.model_calls == []


def test_session_handler_recreates_cached_client_when_git_path_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clients: list[Any] = []
    runtime_ready = {"value": False}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.disconnects = 0
            clients.append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    def inject_git(env, *, base_env, working_dir):
        if runtime_ready["value"]:
            env["PATH"] = f"/managed/git/bin{session_handler_module.os.pathsep}{base_env['PATH']}"
            return True
        return False

    monkeypatch.setenv("PATH", "/gitless/bin")
    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(git_runtime_module, "prepend_vendored_git_to_path", inject_git)
    handler = SessionHandler(_Controller(tmp_path))
    context = MessageContext(user_id="U123", channel_id="C123")

    first_client = _run_session(handler, context)
    runtime_ready["value"] = True
    second_client = _run_session(handler, context)

    assert first_client is not second_client
    assert first_client.disconnects == 1
    assert len(clients) == 2
    assert "PATH" not in first_client.options.env
    assert second_client.options.env["PATH"] == "/managed/git/bin:/gitless/bin"


def test_session_handler_recreates_cached_claude_client_when_caller_env_changes(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {"clients": []}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.disconnects = 0
            captured["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform_specific={
            "task_execution_id": "run-one",
            "task_trigger_kind": "agent_run",
            "agent_session_target": {"id": "ses-parent", "agent_backend": "claude"},
        },
    )

    first_client = _run_session(handler, context)
    context.platform_specific["task_execution_id"] = "run-two"
    second_client = _run_session(handler, context)

    assert first_client is not second_client
    assert first_client.disconnects == 1
    assert len(captured["clients"]) == 2
    assert first_client.options.env["AVIBE_RUN_ID"] == "run-one"
    assert second_client.options.env["AVIBE_RUN_ID"] == "run-two"


def test_session_handler_recreates_cached_claude_subagent_when_caller_env_changes(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {"clients": []}

    class _RoutingSettingsManager(_SettingsManager):
        @staticmethod
        def get_channel_routing(settings_key):
            return type("Routing", (), {"claude_agent": "reviewer", "model": None, "reasoning_effort": None})()

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.disconnects = 0
            captured["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.settings_manager = _RoutingSettingsManager()
    controller.platform_settings_managers = {"slack": controller.settings_manager}
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform_specific={
            "routing_subagent": "reviewer",
            "task_execution_id": "run-one",
            "task_trigger_kind": "agent_run",
            "agent_session_target": {"id": "ses-parent", "agent_backend": "claude"},
        },
    )

    first_client = _run_session(handler, context)
    context.platform_specific["task_execution_id"] = "run-two"
    second_client = _run_session(handler, context)

    assert first_client is not second_client
    assert first_client.disconnects == 1
    assert len(captured["clients"]) == 2
    assert first_client.options.env["AVIBE_RUN_ID"] == "run-one"
    assert second_client.options.env["AVIBE_RUN_ID"] == "run-two"


def test_session_handler_reuses_cached_claude_subagent_after_ensuring_caller_env(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {"clients": []}

    class _RoutingSessions(_Sessions):
        ensured: list[tuple[str, str, str]] = []

        @staticmethod
        def get_agent_session_id(settings_key, base_session_id, agent_name):
            return None

        @classmethod
        def ensure_agent_session_id(cls, settings_key, agent_name, base_session_id, **_kwargs):
            cls.ensured.append((settings_key, agent_name, base_session_id))
            return "ses-subagent"

    class _RoutingSettingsManager(_SettingsManager):
        def __init__(self) -> None:
            super().__init__()
            self.sessions = _RoutingSessions()

        @staticmethod
        def get_channel_routing(settings_key):
            return type("Routing", (), {"claude_agent": "reviewer", "model": None, "reasoning_effort": None})()

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.disconnects = 0
            captured["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.settings_manager = _RoutingSettingsManager()
    controller.platform_settings_managers = {"slack": controller.settings_manager}
    handler = SessionHandler(controller)

    first_context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform_specific={"routing_subagent": "reviewer", "task_trigger_kind": "agent_run"},
    )
    first_client = _run_session(handler, first_context)

    second_context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform_specific={"routing_subagent": "reviewer", "task_trigger_kind": "agent_run"},
    )
    second_client = _run_session(handler, second_context)

    assert first_client is second_client
    assert first_client.disconnects == 0
    assert len(captured["clients"]) == 1
    assert first_client.options.env["AVIBE_SESSION_ID"] == "ses-subagent"
    assert second_context.platform_specific["agent_session_id"] == "ses-subagent"


def test_cached_claude_subagent_revalidates_memory_principal_without_prompt_churn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clients: list[Any] = []

    class _RoutingSessions(_Sessions):
        @staticmethod
        def get_claude_session_id(_settings_key, _base_session_id):
            return None

        @staticmethod
        def get_agent_session_id(_settings_key, _base_session_id, agent_name):
            assert agent_name == "claude"
            return None

        @staticmethod
        def ensure_agent_session_id(
            _settings_key,
            agent_name,
            _base_session_id,
            **_kwargs,
        ):
            assert agent_name == "claude"
            return "ses-subagent"

    class _RoutingSettingsManager(_SettingsManager):
        def __init__(self) -> None:
            super().__init__()
            self.sessions = _RoutingSessions()

        @staticmethod
        def get_channel_routing(_settings_key):
            return type("Routing", (), {"claude_agent": "reviewer", "model": None, "reasoning_effort": None})()

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.disconnects = 0
            clients.append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.config.platform = "avibe"
    controller.config.memory = type("MemoryConfig", (), {"enabled": True})()
    controller.settings_manager = _RoutingSettingsManager()
    controller.platform_settings_managers = {"avibe": controller.settings_manager}
    principals: dict[str, str] = {}

    def configure_memory_cli_session(context, *, admitted):
        session_id = context.platform_specific["agent_session_id"]
        if admitted:
            principals[session_id] = context.user_id
        else:
            principals.pop(session_id, None)
        return admitted

    controller.configure_memory_cli_session = configure_memory_cli_session
    handler = SessionHandler(controller)

    local_context = MessageContext(
        user_id="local",
        channel_id="C123",
        platform="avibe",
        platform_specific={"routing_subagent": "reviewer", "memory_cli_admitted": True},
    )
    local_client = _run_session(handler, local_context)
    assert principals == {"ses-subagent": "local"}
    assert "## Personal Memory" in str(local_client.options.system_prompt)

    remote_context = MessageContext(
        user_id="remote-user",
        channel_id="C123",
        platform="avibe",
        platform_specific={"routing_subagent": "reviewer", "memory_cli_admitted": True},
    )
    remote_client = _run_session(handler, remote_context)
    assert remote_client is local_client
    assert principals == {"ses-subagent": "remote-user"}

    denied_context = MessageContext(
        user_id="remote-user",
        channel_id="C123",
        platform="avibe",
        platform_specific={"routing_subagent": "reviewer"},
    )
    denied_client = _run_session(handler, denied_context)
    assert denied_client is local_client
    assert local_client.disconnects == 0
    assert principals == {}
    assert "## Personal Memory" in str(denied_client.options.system_prompt)


def test_session_handler_recreates_terminated_cached_subagent_before_dispatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {"clients": []}

    class _RoutingSessions(_Sessions):
        @staticmethod
        def get_agent_session_id(settings_key, base_session_id, agent_name):
            return None

        @staticmethod
        def ensure_agent_session_id(settings_key, agent_name, base_session_id, **_kwargs):
            return "ses-subagent"

    class _RoutingSettingsManager(_SettingsManager):
        def __init__(self) -> None:
            super().__init__()
            self.sessions = _RoutingSessions()

        @staticmethod
        def get_channel_routing(settings_key):
            return type("Routing", (), {"claude_agent": "reviewer", "model": None, "reasoning_effort": None})()

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.disconnects = 0
            self._transport = SimpleNamespace(_process=SimpleNamespace(returncode=None))
            captured["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.settings_manager = _RoutingSettingsManager()
    controller.platform_settings_managers = {"slack": controller.settings_manager}
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform_specific={"routing_subagent": "reviewer", "task_trigger_kind": "agent_run"},
    )

    first_client = _run_session(handler, context)
    first_client._transport._process.returncode = -6
    second_client = _run_session(handler, context)

    assert first_client is not second_client
    assert first_client.disconnects == 1
    assert len(captured["clients"]) == 2


def test_session_handler_updates_cached_claude_model_only_when_changed(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options
            self.model_calls = []

        async def connect(self) -> None:
            captured["connected"] = True

        async def set_model(self, model) -> None:
            self.model_calls.append(model)

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform_specific={"agent_session_target": {"model": "claude-sonnet-4-5"}},
    )

    client = _run_session(handler, context)
    context.platform_specific["agent_session_target"]["model"] = "claude-opus-4-1"

    _run_session(handler, context)
    _run_session(handler, context)

    assert client.model_calls == ["claude-opus-4-1"]


def test_session_handler_does_not_send_none_model_control_request_for_cached_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options
            self.model_calls = []

        async def connect(self) -> None:
            captured["connected"] = True

        async def set_model(self, model) -> None:
            self.model_calls.append(model)

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    client = _run_session(handler, context)
    _run_session(handler, context)

    assert captured["options"].extra_args == {"replay-user-messages": None}
    assert client.model_calls == []


def test_session_handler_passes_non_default_claude_command_name(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.config.claude.cli_path = "claude-proxy"
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    assert captured["connected"] is True
    assert captured["options"].cli_path == "claude-proxy"


def test_session_handler_expands_tilde_in_claude_cli_path(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.config.claude.cli_path = "~/bin/claude"
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    assert captured["connected"] is True
    assert captured["options"].cli_path == str(Path("~/bin/claude").expanduser())


def test_session_handler_surfaces_claude_missing_resume_session(monkeypatch, tmp_path: Path) -> None:
    stale_session_id = "11111111-1111-1111-1111-111111111111"
    captured: dict[str, Any] = {}

    class _StaleSessions:
        @staticmethod
        def get_claude_session_id(settings_key, base_session_id):
            assert settings_key == "test::C123"
            assert base_session_id == "slack_C123"
            return stale_session_id

        @staticmethod
        def get_agent_session_id(settings_key, base_session_id, agent_name):
            return None

        @staticmethod
        def ensure_agent_session_id(settings_key, agent_name, base_session_id, **_kwargs):
            return "sesk8m4q2p7x"

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["options"].stderr(f"No conversation found with session ID: {stale_session_id}")
            raise RuntimeError("Command failed with exit code 1")

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.settings_manager.sessions = _StaleSessions()
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    with pytest.raises(session_handler_module.ClaudeSessionNotFoundError) as exc_info:
        _run_session(handler, context)

    assert exc_info.value.session_id == stale_session_id
    assert exc_info.value.working_path == str(tmp_path)
    assert stale_session_id in exc_info.value.stderr
    assert captured["options"].resume == stale_session_id


def test_claude_startup_failure_is_recorded_before_scope_retirement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from modules.agents.model_hub import ModelHubLaunch

    events: list[tuple] = []

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.options = options

        async def connect(self) -> None:
            raise RuntimeError("startup failed")

    class _Runtime:
        async def resolve(self, backend, requested_model, **kwargs):
            return ModelHubLaunch(
                backend=backend,
                channel="native_cli",
                requested_model=requested_model,
                target_model="claude-opus",
                runtime_model="claude-opus",
                source_id="src_native01",
            )

        async def record_native_failure(self, context, diagnostic):
            events.append(("record", diagnostic))
            return False

        def retire_process_scope(
            self,
            backend,
            process_scope,
            *,
            terminal_turn_id=None,
        ):
            events.append(
                (
                    "retire",
                    backend,
                    process_scope,
                    terminal_turn_id,
                )
            )

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    controller.model_hub_runtime = _Runtime()
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform_specific={"turn_token": "turn_startup_failure"},
    )

    with pytest.raises(RuntimeError, match="startup failed"):
        _run_session(handler, context)

    assert events == [
        ("record", "startup failed"),
        (
            "retire",
            "claude",
            f"slack_C123:{tmp_path}",
            "turn_startup_failure",
        ),
    ]


def test_session_handler_uses_scheduled_turn_source_for_dm_anchor(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _ScheduledSessions:
        def __init__(self) -> None:
            self.lookup = None

        def get_claude_session_id(self, settings_key, base_session_id):
            self.lookup = (settings_key, base_session_id)
            return None

        @staticmethod
        def get_agent_session_id(settings_key, base_session_id, agent_name):
            return None

        @staticmethod
        def ensure_agent_session_id(settings_key, agent_name, base_session_id, **_kwargs):
            return "sesk8m4q2p7x"

    class _ScheduledSettingsManager:
        def __init__(self) -> None:
            self.sessions = _ScheduledSessions()

        @staticmethod
        def get_channel_settings(settings_key):
            return None

        @staticmethod
        def get_channel_routing(settings_key):
            return None

    class _ScheduledController:
        def __init__(self, working_path: Path) -> None:
            self.config = _Config()
            self.im_client = type(
                "IM",
                (),
                {
                    "formatter": None,
                    "should_use_thread_for_dm_session": lambda self: True,
                    "should_use_thread_for_reply": lambda self: True,
                },
            )()
            self.settings_manager = _ScheduledSettingsManager()
            self.platform_settings_managers = {"slack": self.settings_manager}
            self.session_manager = object()
            self.claude_sessions = {}
            self.receiver_tasks = {}
            self.stored_session_mappings = {}
            self._working_path = working_path

        def get_cwd(self, context) -> str:
            return str(self._working_path)

        @staticmethod
        def _get_settings_key(context) -> str:
            return context.user_id if (context.platform_specific or {}).get("is_dm") else context.channel_id

        @staticmethod
        def _get_session_key(context) -> str:
            settings_key = _ScheduledController._get_settings_key(context)
            return f"{getattr(context, 'platform', None) or 'test'}::{settings_key}"

        def get_settings_manager_for_context(self, context=None):
            return self.settings_manager

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _ScheduledController(tmp_path)
    handler = SessionHandler(controller)
    precomputed_base = "slack_scheduled-anchor-123"
    context = MessageContext(
        user_id="U123",
        channel_id="D123",
        message_id="scheduled:task-1:exec-1",
        platform="slack",
        platform_specific={
            "is_dm": True,
            "turn_source": "scheduled",
            "turn_base_session_id": precomputed_base,
        },
    )

    client = _run_session(handler, context)

    assert captured["connected"] is True
    assert controller.settings_manager.sessions.lookup is not None
    settings_key, base_session_id = controller.settings_manager.sessions.lookup
    assert settings_key == "slack::U123"
    assert base_session_id == precomputed_base
    assert getattr(client, "_vibe_runtime_base_session_id") == base_session_id
    assert getattr(client, "_vibe_runtime_session_key") == f"{base_session_id}:{tmp_path}"


def test_session_handler_evicts_idle_claude_session(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options
            captured["disconnects"] = 0

        async def connect(self) -> None:
            captured["connected"] = True

        async def disconnect(self) -> None:
            captured["disconnects"] += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    controller.runtime_activation = RuntimeActivationRegistry()
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    client = _run_session(handler, context)

    composite_key = f"slack_C123:{tmp_path}"
    identity = getattr(client, "_vibe_runtime_activation_identity")
    handler.session_last_activity[composite_key] = 0.0

    evicted = asyncio.run(handler.evict_idle_sessions(600))

    assert evicted == 1
    assert captured["disconnects"] == 1
    assert composite_key not in controller.claude_sessions
    assert composite_key not in handler.session_last_activity
    assert controller.runtime_activation.current("claude", composite_key) is None
    assert controller.runtime_activation.current(
        "claude",
        composite_key,
        include_retired=True,
    ) == identity


def test_idle_sweep_batches_ownership_reads_off_the_controller_loop(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)
    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    loop_thread = threading.get_ident()
    batch_threads: list[int] = []
    single_calls: list[str] = []

    def ownership(resource_key: str):
        return SimpleNamespace(
            resource_key=resource_key,
            disposition=SessionRuntimeDisposition.RECLAIMABLE,
            blocks_reclamation=False,
            needs_session_delivery_wake=False,
            needs_request_wake=False,
        )

    class _Provider:
        def snapshot(self, target):
            single_calls.append(target.resource_key)
            return ownership(target.resource_key)

        def snapshot_many(self, targets):
            batch_threads.append(threading.get_ident())
            return tuple(ownership(target.resource_key) for target in targets)

    controller.runtime_ownership = _Provider()
    for suffix in ("a", "b"):
        resource_key = f"runtime-{suffix}"
        controller.claude_sessions[resource_key] = SimpleNamespace(
            _vibe_runtime_base_session_id=f"base-{suffix}",
            _vibe_runtime_session_key=resource_key,
            _vibe_runtime_workdir=f"/work/{suffix}",
            _vibe_runtime_fallback_session_key=f"route:{suffix}",
            _vibe_agent_session_id=f"ses-{suffix}",
        )
        handler.session_last_activity[resource_key] = 999.0

    assert asyncio.run(handler.evict_idle_sessions(600)) == 0
    assert single_calls == []
    assert len(batch_threads) == 1
    assert batch_threads[0] != loop_thread


def test_session_handler_keeps_active_claude_session(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options
            captured["disconnects"] = 0

        async def connect(self) -> None:
            captured["connected"] = True

        async def disconnect(self) -> None:
            captured["disconnects"] += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    composite_key = f"slack_C123:{tmp_path}"
    handler.session_last_activity[composite_key] = 0.0
    handler.active_sessions.add(composite_key)

    evicted = asyncio.run(handler.evict_idle_sessions(600))

    assert evicted == 0
    assert captured["disconnects"] == 0
    assert composite_key in controller.claude_sessions


def test_evict_idle_sessions_force_evicts_stuck_active_session(monkeypatch, tmp_path: Path) -> None:
    """The active flag is not an absolute veto.

    Regression for the no-EOF / blocked-receiver leak: a receiver coroutine that
    stays alive but blocked never releases the per-turn ``active`` flag, so the
    session is pinned in ``active_sessions`` forever and its ``last_activity`` is
    frozen. Once that frozen activity is older than the absolute cap
    (``idle_timeout * multiplier``), the backstop must force-evict it. This is
    distinct from the stream-exhausted path covered elsewhere, which relies on
    the receiver actually terminating to release the flag.
    """
    captured: dict[str, Any] = {}

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _disconnect_counting_client(captured))
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    cleanup_calls: list[str] = []

    class _ClaudeAgent:
        @staticmethod
        async def force_cleanup_stuck_active_session(
            composite_key: str,
            *,
            runtime_lock_held: bool = False,
        ) -> None:
            assert runtime_lock_held is True
            cleanup_calls.append(composite_key)
            handler.clear_session_tracking(composite_key)

    controller.agent_service = type("AgentService", (), {"agents": {"claude": _ClaudeAgent()}})()
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    composite_key = f"slack_C123:{tmp_path}"
    # Stuck-active: active flag set, but activity frozen 2000s ago. With the
    # default 3x multiplier the cap is 1800s, so 2000s > cap -> force-evict.
    handler.session_last_activity[composite_key] = -1000.0
    handler.active_sessions.add(composite_key)

    evicted = asyncio.run(handler.evict_idle_sessions(600))

    assert evicted == 1
    assert cleanup_calls == [composite_key]
    assert captured["disconnects"] == 0
    assert composite_key in controller.claude_sessions
    assert composite_key not in handler.active_sessions


def test_evict_idle_sessions_keeps_stuck_active_below_cap(monkeypatch, tmp_path: Path) -> None:
    """An active session below the absolute cap is still protected."""
    captured: dict[str, Any] = {}

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _disconnect_counting_client(captured))
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    composite_key = f"slack_C123:{tmp_path}"
    # Idle for 1700s: past idle_timeout (600) but below the 1800s absolute cap.
    handler.session_last_activity[composite_key] = -700.0
    handler.active_sessions.add(composite_key)

    evicted = asyncio.run(handler.evict_idle_sessions(600))

    assert evicted == 0
    assert captured["disconnects"] == 0
    assert composite_key in controller.claude_sessions
    assert composite_key in handler.active_sessions


def test_evict_idle_sessions_stuck_cap_floor_dominates_small_timeout(monkeypatch, tmp_path: Path) -> None:
    """A tiny idle timeout must not shrink the active-turn grace below 30min."""
    captured: dict[str, Any] = {}

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _disconnect_counting_client(captured))
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    composite_key = f"slack_C123:{tmp_path}"
    # idle_timeout=60 would make a 180s multiplier window; the 1800s floor
    # dominates, so 1000s of quiet active time stays protected.
    handler.session_last_activity[composite_key] = 0.0
    handler.active_sessions.add(composite_key)

    evicted = asyncio.run(handler.evict_idle_sessions(60))

    assert evicted == 0
    assert captured["disconnects"] == 0
    assert composite_key in controller.claude_sessions
    assert composite_key in handler.active_sessions


def test_evict_idle_sessions_stuck_active_backstop_can_be_disabled(monkeypatch, tmp_path: Path) -> None:
    """``stuck_active_multiplier <= 0`` restores the absolute active veto."""
    captured: dict[str, Any] = {}

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _disconnect_counting_client(captured))
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    composite_key = f"slack_C123:{tmp_path}"
    handler.session_last_activity[composite_key] = -100000.0
    handler.active_sessions.add(composite_key)

    evicted = asyncio.run(handler.evict_idle_sessions(600, stuck_active_multiplier=0))

    assert evicted == 0
    assert captured["disconnects"] == 0
    assert composite_key in controller.claude_sessions


def test_evict_idle_sessions_spares_session_refreshed_between_passes(monkeypatch, tmp_path: Path) -> None:
    """The recheck pass re-reads ``last_activity`` from current state.

    A session that looked idle in the collect pass but was touched (a new
    message arrived) before the recheck pass must NOT be evicted.
    """
    captured: dict[str, Any] = {}

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _disconnect_counting_client(captured))
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    composite_key = f"slack_C123:{tmp_path}"

    class _RefreshingActivity(dict):
        # Collect pass iterates .items() and sees the stale 0.0 (idle 1000s);
        # the recheck pass calls .get() and sees a freshly-touched 900.0
        # (idle 100s < idle_timeout), so the session must be spared.
        def get(self, key, default=None):
            return 900.0

    handler.session_last_activity = _RefreshingActivity({composite_key: 0.0})

    evicted = asyncio.run(handler.evict_idle_sessions(600))

    assert evicted == 0
    assert captured["disconnects"] == 0
    assert composite_key in controller.claude_sessions


def test_evict_idle_sessions_evicts_stuck_active_deactivated_between_passes(monkeypatch, tmp_path: Path) -> None:
    """Stuck-active in the collect pass, deactivated before recheck.

    It must still be evicted — via the normal idle path — since by the recheck
    pass it is no longer active and is well past ``idle_timeout``.
    """
    captured: dict[str, Any] = {}

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _disconnect_counting_client(captured))
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    composite_key = f"slack_C123:{tmp_path}"
    # Idle 2000s (>= 1800 stuck cap and >= 600 idle_timeout).
    handler.session_last_activity[composite_key] = -1000.0

    class _DeactivatingActiveSet(set):
        def __init__(self, target_key: str):
            super().__init__()
            self.target_key = target_key
            self.add(target_key)
            self._checks = 0

        def __contains__(self, item):
            if item == self.target_key:
                self._checks += 1
                # active in the collect pass, deactivated by the recheck pass
                return self._checks < 2
            return super().__contains__(item)

    handler.active_sessions = _DeactivatingActiveSet(composite_key)

    evicted = asyncio.run(handler.evict_idle_sessions(600))

    assert evicted == 1
    assert captured["disconnects"] == 1
    assert composite_key not in controller.claude_sessions


def test_reap_orphaned_sessions_disables_in_tree_sweep_when_pid_unresolved(monkeypatch, tmp_path: Path) -> None:
    """If a tracked client's pid cannot be resolved, the in-tree sweep is
    disabled so the live process is not misclassified as an orphan."""
    captured: dict[str, Any] = {}

    async def _fake_reap(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(session_handler_module, "reap_orphaned_claude_processes", _fake_reap)
    monkeypatch.setattr(session_handler_module, "get_claude_client_pid", lambda client: None)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    composite_key = f"slack_C123:{tmp_path}"
    controller.claude_sessions[composite_key] = object()  # tracked but pid unresolved

    asyncio.run(handler.reap_orphaned_claude_sessions())

    assert captured["reap_in_tree"] is False


def test_reap_orphaned_sessions_disables_in_tree_sweep_when_create_in_flight(monkeypatch, tmp_path: Path) -> None:
    """A session create in flight (subprocess spawned, not yet tracked) disables
    the in-tree sweep."""
    captured: dict[str, Any] = {}

    async def _fake_reap(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(session_handler_module, "reap_orphaned_claude_processes", _fake_reap)
    monkeypatch.setattr(session_handler_module, "get_claude_client_pid", lambda client: 4321)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    handler.claude_session_creates["slack_C123:/in/flight"] = object()

    asyncio.run(handler.reap_orphaned_claude_sessions())

    assert captured["reap_in_tree"] is False


def test_reap_orphaned_sessions_enables_in_tree_sweep_when_owner_set_complete(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def _fake_reap(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(session_handler_module, "reap_orphaned_claude_processes", _fake_reap)
    monkeypatch.setattr(session_handler_module, "get_claude_client_pid", lambda client: 4321)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    controller.claude_sessions[f"slack_C123:{tmp_path}"] = object()

    asyncio.run(handler.reap_orphaned_claude_sessions())

    assert captured["reap_in_tree"] is True


def test_reap_orphaned_sessions_excludes_active_watch_process_roots(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def _fake_reap(**kwargs):
        captured.update(kwargs)
        return 0

    class _WatchService:
        @staticmethod
        def active_process_pids():
            return {500}

    monkeypatch.setattr(session_handler_module, "reap_orphaned_claude_processes", _fake_reap)
    monkeypatch.setattr(session_handler_module, "get_claude_client_pid", lambda client: 4321)

    controller = _Controller(tmp_path)
    controller.watch_service = _WatchService()
    handler = SessionHandler(controller)
    controller.claude_sessions[f"slack_C123:{tmp_path}"] = object()

    asyncio.run(handler.reap_orphaned_claude_sessions())

    assert captured["exclude_pids"] == {500}


def test_reap_orphaned_sessions_excludes_active_claude_auth_clients(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def _fake_reap(**kwargs):
        captured.update(kwargs)
        return 0

    class _AuthService:
        @staticmethod
        def active_claude_auth_client_pids():
            return {600}

    monkeypatch.setattr(session_handler_module, "reap_orphaned_claude_processes", _fake_reap)
    monkeypatch.setattr(session_handler_module, "get_claude_client_pid", lambda client: 4321)

    controller = _Controller(tmp_path)
    controller.agent_auth_service = _AuthService()
    handler = SessionHandler(controller)
    controller.claude_sessions[f"slack_C123:{tmp_path}"] = object()

    asyncio.run(handler.reap_orphaned_claude_sessions())

    assert captured["exclude_pids"] == {600}


def test_reap_orphaned_sessions_disables_in_tree_when_auth_client_pid_unknown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_reap(**kwargs):
        captured.update(kwargs)
        return 0

    class _AuthService:
        @staticmethod
        def active_claude_auth_client_pids():
            return set()

        @staticmethod
        def has_active_claude_auth_client_with_unknown_pid():
            return True

    monkeypatch.setattr(session_handler_module, "reap_orphaned_claude_processes", _fake_reap)
    monkeypatch.setattr(session_handler_module, "get_claude_client_pid", lambda client: 4321)

    controller = _Controller(tmp_path)
    controller.agent_auth_service = _AuthService()
    handler = SessionHandler(controller)
    controller.claude_sessions[f"slack_C123:{tmp_path}"] = object()

    asyncio.run(handler.reap_orphaned_claude_sessions())

    assert captured["reap_in_tree"] is False


def test_cleanup_session_logs_exact_runtime_generation(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        session_handler_module,
        "ClaudeAgentOptions",
        _StubClaudeAgentOptions,
    )
    monkeypatch.setattr(
        session_handler_module,
        "ClaudeSDKClient",
        _disconnect_counting_client(captured),
    )

    controller = _Controller(tmp_path)
    controller.runtime_activation = RuntimeActivationRegistry()
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    client = _run_session(handler, context)
    composite_key = f"slack_C123:{tmp_path}"
    identity = getattr(client, "_vibe_runtime_activation_identity")

    async def _exercise_cleanup() -> int:
        receiver = asyncio.create_task(asyncio.sleep(3600))
        controller.receiver_tasks[composite_key] = receiver
        handler.mark_session_active(composite_key)
        receiver_identity = id(receiver)
        with caplog.at_level("INFO", logger="core.handlers.session_handler"):
            await handler.cleanup_session(
                composite_key,
                reason="test_requested_teardown",
            )
        return receiver_identity

    receiver_identity = asyncio.run(_exercise_cleanup())

    evidence = caplog.text
    assert f"session={composite_key}" in evidence
    assert "reason=test_requested_teardown" in evidence
    assert "busy=True" in evidence
    assert f"runtime_generation={identity.generation}" in evidence
    assert f"client_identity={id(client)}" in evidence
    assert f"receiver_identity={receiver_identity}" in evidence
    assert "receiver_done=False" in evidence


def test_cleanup_session_swallows_cancelled_receiver_task(monkeypatch, tmp_path: Path) -> None:
    events = []

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            events.append("disconnect")
            self.disconnects += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    client = _run_session(handler, context)
    composite_key = f"slack_C123:{tmp_path}"

    async def _exercise_cleanup() -> None:
        async def _receiver():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                events.append("cancel")
                raise

        controller.receiver_tasks[composite_key] = asyncio.create_task(_receiver())
        await asyncio.sleep(0)
        await handler.cleanup_session(composite_key)

    asyncio.run(_exercise_cleanup())

    assert client.disconnects == 1
    assert events == ["disconnect", "cancel"]
    assert composite_key not in controller.receiver_tasks
    assert composite_key not in controller.claude_sessions


def test_cleanup_session_retires_model_hub_process_scope(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _StubClaudeSDKClient:
        def __init__(self, options):
            pass

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(
        session_handler_module,
        "ClaudeAgentOptions",
        _StubClaudeAgentOptions,
    )
    monkeypatch.setattr(
        session_handler_module,
        "ClaudeSDKClient",
        _StubClaudeSDKClient,
    )

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    _run_session(handler, context)
    composite_key = f"slack_C123:{tmp_path}"
    retired: list[tuple[str, str]] = []
    controller.model_hub_runtime = SimpleNamespace(
        retire_process_scope=lambda backend, scope: retired.append(
            (backend, scope)
        )
    )

    asyncio.run(handler.cleanup_session(composite_key))

    assert retired == [("claude", composite_key)]


def test_cleanup_session_swallows_receiver_task_failure(monkeypatch, tmp_path: Path) -> None:
    events = []
    disconnected = asyncio.Event()

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            events.append("disconnect")
            self.disconnects += 1
            disconnected.set()

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    client = _run_session(handler, context)
    composite_key = f"slack_C123:{tmp_path}"

    async def _exercise_cleanup() -> None:
        async def _receiver():
            await disconnected.wait()
            events.append("receiver-error")
            raise RuntimeError("receiver failed")

        controller.receiver_tasks[composite_key] = asyncio.create_task(_receiver())
        await asyncio.sleep(0)
        await handler.cleanup_session(composite_key)

    asyncio.run(_exercise_cleanup())

    assert client.disconnects == 1
    assert events == ["disconnect", "receiver-error"]
    assert composite_key not in controller.receiver_tasks
    assert composite_key not in controller.claude_sessions


def test_cleanup_session_drains_finished_receiver_task_failure(monkeypatch, tmp_path: Path) -> None:
    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    class _DoneReceiverTask:
        drained = False

        @staticmethod
        def done():
            return True

        def exception(self):
            self.drained = True
            return RuntimeError("receiver already failed")

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    client = _run_session(handler, context)
    composite_key = f"slack_C123:{tmp_path}"
    receiver_task = _DoneReceiverTask()
    controller.receiver_tasks[composite_key] = receiver_task

    asyncio.run(handler.cleanup_session(composite_key))

    assert client.disconnects == 1
    assert receiver_task.drained
    assert composite_key not in controller.receiver_tasks
    assert composite_key not in controller.claude_sessions


def test_cleanup_session_cancels_receiver_when_disconnect_is_cancelled(monkeypatch, tmp_path: Path) -> None:
    events = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1
            events["disconnect_started"].set()
            await asyncio.Future()

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    composite_key = f"slack_C123:{tmp_path}"

    async def _exercise_cleanup() -> None:
        events["disconnect_started"] = asyncio.Event()
        events["receiver_cancelled"] = asyncio.Event()
        client = await handler.get_or_create_claude_session(context)

        async def _receiver():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                events["receiver_cancelled"].set()
                raise

        receiver_task = asyncio.create_task(_receiver())
        controller.receiver_tasks[composite_key] = receiver_task
        cleanup_task = asyncio.create_task(handler.cleanup_session(composite_key))

        await events["disconnect_started"].wait()
        assert composite_key not in controller.receiver_tasks

        cleanup_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup_task

        assert client.disconnects == 1
        assert events["receiver_cancelled"].is_set()

    asyncio.run(_exercise_cleanup())

    assert composite_key not in controller.receiver_tasks
    assert composite_key not in controller.claude_sessions


def test_cleanup_session_preserves_new_receiver_during_disconnect(monkeypatch, tmp_path: Path) -> None:
    events = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1
            events["disconnect_started"].set()
            await asyncio.Future()

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    composite_key = f"slack_C123:{tmp_path}"

    async def _exercise_cleanup() -> None:
        events["disconnect_started"] = asyncio.Event()
        events["old_receiver_cancelled"] = asyncio.Event()
        await handler.get_or_create_claude_session(context)

        async def _old_receiver():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                events["old_receiver_cancelled"].set()
                raise

        old_receiver = asyncio.create_task(_old_receiver())
        new_receiver = asyncio.create_task(asyncio.sleep(3600))
        controller.receiver_tasks[composite_key] = old_receiver
        handler.mark_session_active(composite_key)
        cleanup_task = asyncio.create_task(handler.cleanup_session(composite_key))

        await events["disconnect_started"].wait()
        assert composite_key not in controller.receiver_tasks
        assert composite_key not in handler.active_sessions
        assert composite_key not in handler.session_last_activity
        controller.receiver_tasks[composite_key] = new_receiver
        handler.mark_session_active(composite_key)

        cleanup_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup_task

        assert events["old_receiver_cancelled"].is_set()
        assert controller.receiver_tasks[composite_key] is new_receiver
        assert composite_key in handler.active_sessions
        assert composite_key not in handler.session_last_activity
        new_receiver.cancel()
        with pytest.raises(asyncio.CancelledError):
            await new_receiver

    asyncio.run(_exercise_cleanup())

    assert composite_key in controller.receiver_tasks
    controller.receiver_tasks.pop(composite_key, None)
    assert composite_key not in controller.claude_sessions


def test_cleanup_session_defers_disconnect_for_current_receiver(monkeypatch, tmp_path: Path) -> None:
    events = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1
            events["disconnect_started"].set()
            await events["release_disconnect"].wait()

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    composite_key = f"slack_C123:{tmp_path}"

    async def _exercise_cleanup() -> None:
        events["cleanup_returned"] = asyncio.Event()
        events["disconnect_started"] = asyncio.Event()
        events["release_disconnect"] = asyncio.Event()
        client = await handler.get_or_create_claude_session(context)

        async def _receiver():
            await handler.cleanup_session(
                composite_key,
                current_receiver_task=asyncio.current_task(),
            )
            events["cleanup_returned"].set()

        receiver_task = asyncio.create_task(_receiver())
        controller.receiver_tasks[composite_key] = receiver_task

        await events["cleanup_returned"].wait()
        assert composite_key not in controller.receiver_tasks
        assert composite_key not in controller.claude_sessions

        await events["disconnect_started"].wait()
        assert client.disconnects == 1
        events["release_disconnect"].set()
        await asyncio.sleep(0)

    asyncio.run(_exercise_cleanup())

    assert composite_key not in controller.receiver_tasks
    assert composite_key not in controller.claude_sessions


def test_evict_idle_sessions_rechecks_active_state_before_cleanup(monkeypatch, tmp_path: Path) -> None:
    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    class _FlippingActiveSet(set):
        def __init__(self, target_key: str):
            super().__init__()
            self.target_key = target_key
            self._checks = 0

        def __contains__(self, item):
            if item == self.target_key:
                self._checks += 1
                return self._checks >= 2
            return super().__contains__(item)

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    client = _run_session(handler, context)
    composite_key = f"slack_C123:{tmp_path}"
    handler.session_last_activity[composite_key] = 0.0
    handler.active_sessions = _FlippingActiveSet(composite_key)

    evicted = asyncio.run(handler.evict_idle_sessions(600))

    assert evicted == 0
    assert client.disconnects == 0
    assert composite_key in controller.claude_sessions


def test_evict_idle_sessions_reaps_native_resume_processes(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {"reap_calls": []}

    class _StubSessions(_Sessions):
        @staticmethod
        def get_claude_session_id(settings_key, base_session_id):
            assert settings_key == "test::C123"
            assert base_session_id == "slack_C123"
            return "native-session-1"

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0
            self._transport = type(
                "Transport",
                (),
                {"_process": type("Process", (), {"pid": 4321})()},
            )()
            captured["client"] = self

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    async def fake_reap(
        native_session_id,
        *,
        keep_pid=None,
        exclude_pids=None,
        cli_path=None,
        logger,
        terminate_timeout=2.0,
    ):
        captured["reap_calls"].append((native_session_id, keep_pid, cli_path))
        captured["exclude_pids"] = exclude_pids
        return 2

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(session_handler_module, "reap_duplicate_claude_resume_processes", fake_reap)
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    controller.settings_manager.sessions = _StubSessions()
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    client = _run_session(handler, context)
    composite_key = f"slack_C123:{tmp_path}"
    handler.session_last_activity[composite_key] = 0.0

    evicted = asyncio.run(handler.evict_idle_sessions(600))

    assert evicted == 1
    assert client.disconnects == 1
    assert getattr(client, "_vibe_native_session_id") == "native-session-1"
    assert captured["reap_calls"] == [("native-session-1", None, "/usr/local/bin/claude-proxy")]
    # The evicted client is gone from the registry, so its own pid stays reapable.
    assert captured["exclude_pids"] == set()


def test_cleanup_defers_duplicate_reap_while_a_client_create_is_in_flight(
    monkeypatch, tmp_path: Path
) -> None:
    """An in-flight create owns a subprocess that is not yet registered.

    Generation locks are per composite key, so another key can be inside
    ``connect()`` for the same native resume id. Its pid cannot appear in
    ``claude_sessions`` yet, so the reap must be deferred instead of running
    against a knowingly incomplete owner set.
    """
    captured: dict[str, Any] = {"reap_calls": 0}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0
            self._transport = type(
                "Transport",
                (),
                {"_process": type("Process", (), {"pid": 4321})()},
            )()

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    async def fake_reap(*args, **kwargs):
        captured["reap_calls"] += 1
        return 0

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(session_handler_module, "reap_duplicate_claude_resume_processes", fake_reap)
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    client = _run_session(handler, context)
    composite_key = f"slack_C123:{tmp_path}"
    setattr(client, "_vibe_native_session_id", "native-session-1")
    handler.claude_session_creates["slack_C999:other"] = object()

    asyncio.run(handler.cleanup_session(composite_key))

    assert captured["reap_calls"] == 0
    assert client.disconnects == 1


def test_evict_idle_sessions_protects_pids_of_other_live_sessions(monkeypatch, tmp_path: Path) -> None:
    """The duplicate reap must never target a still-registered client's pid.

    ``force_cleanup_stuck_active_session`` reaches cleanup without a receiver
    task, so ``keep_pid`` is dropped and the reaper falls back to "kill every
    process matching this native session id". Ownership of live pids is the
    guard that survives that path.
    """
    captured: dict[str, Any] = {}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0
            self._transport = type(
                "Transport",
                (),
                {"_process": type("Process", (), {"pid": 4321})()},
            )()

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    async def fake_reap(
        native_session_id,
        *,
        keep_pid=None,
        exclude_pids=None,
        cli_path=None,
        logger,
        terminate_timeout=2.0,
    ):
        captured["exclude_pids"] = exclude_pids
        return 0

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(session_handler_module, "reap_duplicate_claude_resume_processes", fake_reap)
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    client = _run_session(handler, context)
    composite_key = f"slack_C123:{tmp_path}"
    setattr(client, "_vibe_native_session_id", "native-session-1")

    # A second live client resuming the same native session id, as created by
    # the turn that follows a force eviction.
    replacement = _StubClaudeSDKClient(None)
    replacement._transport._process.pid = 9876
    setattr(replacement, "_vibe_native_session_id", "native-session-1")
    controller.claude_sessions["slack_C999:other"] = replacement

    asyncio.run(handler.cleanup_session(composite_key))

    assert captured["exclude_pids"] == {9876}


def test_cleanup_defers_duplicate_reap_when_a_live_client_pid_is_unresolved(
    monkeypatch, tmp_path: Path
) -> None:
    """An owner whose pid cannot be read still owns its process.

    ``_live_claude_client_pids`` can only name the pids it can resolve. If a
    registered client is not among them the exclusion set is knowingly partial,
    and running the process-table scan against it can select that live client's
    process. ``reap_orphaned_claude_sessions`` already defers on the same
    signal; duplicate cleanup must not treat a partial set as authoritative.
    """
    captured: dict[str, Any] = {"reap_calls": 0}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0
            self._transport = type(
                "Transport",
                (),
                {"_process": type("Process", (), {"pid": 4321})()},
            )()

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    async def fake_reap(*args, **kwargs):
        captured["reap_calls"] += 1
        return 0

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(session_handler_module, "reap_duplicate_claude_resume_processes", fake_reap)
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    client = _run_session(handler, context)
    composite_key = f"slack_C123:{tmp_path}"
    setattr(client, "_vibe_native_session_id", "native-session-1")

    # Registered, live, and resuming the same native id — but its pid cannot be
    # resolved, so it cannot be named in ``exclude_pids``.
    replacement = _StubClaudeSDKClient(None)
    replacement._transport._process.pid = None
    setattr(replacement, "_vibe_native_session_id", "native-session-1")
    controller.claude_sessions["slack_C999:other"] = replacement

    asyncio.run(handler.cleanup_session(composite_key))

    assert captured["reap_calls"] == 0
    assert client.disconnects == 1


def test_cleanup_skips_a_generation_a_replacement_already_took_over(
    monkeypatch, tmp_path: Path
) -> None:
    """Containing a stale teardown must not become a second teardown.

    ``cleanup_session`` resolves the composite key again under the generation
    lock. A caller acting on a client that has since been replaced would
    otherwise disconnect the healthy replacement that now owns the key.
    """
    captured: dict[str, Any] = {"reap_calls": 0}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            self.disconnects = 0
            self._transport = type(
                "Transport",
                (),
                {"_process": type("Process", (), {"pid": 4321})()},
            )()

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnects += 1

    async def fake_reap(*args, **kwargs):
        captured["reap_calls"] += 1
        return 0

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(session_handler_module, "reap_duplicate_claude_resume_processes", fake_reap)
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")
    superseded = _run_session(handler, context)
    composite_key = f"slack_C123:{tmp_path}"
    setattr(superseded, "_vibe_native_session_id", "native-session-1")

    # A newer turn registered its own client under the same key.
    replacement = _StubClaudeSDKClient(None)
    setattr(replacement, "_vibe_native_session_id", "native-session-1")
    controller.claude_sessions[composite_key] = replacement

    asyncio.run(handler.cleanup_session(composite_key, expected_client=superseded))

    assert replacement.disconnects == 0
    assert superseded.disconnects == 0
    assert captured["reap_calls"] == 0
    assert controller.claude_sessions[composite_key] is replacement


def test_cleanup_records_no_teardown_intent_for_an_empty_key(monkeypatch, tmp_path: Path) -> None:
    """No generation retired means nothing to explain later.

    A marker left on an already-empty key opens a 120s window in which the NEXT
    client's genuine ``-9`` — dying inside ``connect()``, before registration
    can clear the record — is suppressed as a service teardown.
    """
    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    composite_key = f"slack_C123:{tmp_path}"

    asyncio.run(handler.cleanup_session(composite_key))

    assert composite_key not in handler.claude_intentional_teardowns


def test_tracking_a_create_retires_the_previous_teardown_record(monkeypatch, tmp_path: Path) -> None:
    """The marker must not outlive the start of the replacement's creation.

    A new generation that exits ``-9`` inside ``connect()`` never registers, so
    its failure is reported with ``client=None``. Clearing only at registration
    would leave the previous teardown's key-level marker standing to classify
    that genuine crash as our own cleanup and swallow the user-facing error.
    """
    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: 1000.0)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    composite_key = f"slack_C123:{tmp_path}"
    handler.claude_intentional_teardowns[composite_key] = 1000.0

    async def scenario() -> bool:
        handler._track_claude_session_create(composite_key)
        # No client was ever handed back, so the caller has none to pass.
        return handler.claude_teardown_is_intentional(
            composite_key, RuntimeError("Claude Code process exited with exit code: -9")
        )

    assert asyncio.run(scenario()) is False
    assert composite_key not in handler.claude_intentional_teardowns
