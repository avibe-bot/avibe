from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.handlers.session_handler as session_handler_module
from config.v2_compat import to_app_config
from config.v2_config import AgentsConfig, ClaudeConfig, RuntimeConfig, SlackConfig, V2Config
from core.handlers.session_handler import SessionHandler
from modules.claude_sdk_compat import CLAUDE_SDK_MAX_BUFFER_SIZE
from modules.im import MessageContext


@dataclass
class _ClaudeRuntimeConfig:
    permission_mode: str = "bypassPermissions"
    cwd: str = "/tmp/workdir"
    system_prompt: str | None = None
    default_model: str | None = None
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
    def ensure_agent_session_id(settings_key, agent_name, base_session_id):
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
    assert controller.claude_sessions[f"slack_C123:{tmp_path}"] is client
    assert getattr(client, "_vibe_runtime_base_session_id") == "slack_C123"
    assert getattr(client, "_vibe_runtime_session_key") == f"slack_C123:{tmp_path}"


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
    assert captured["options"].extra_args == {"model": "claude-sonnet-4-5"}
    assert captured["options"].effort == "high"
    assert not hasattr(client, "_vibe_native_session_id")
    prompt_value = captured["options"].system_prompt
    prompt = prompt_value["append"] if isinstance(prompt_value, dict) else prompt_value
    assert "Current session id: `ses-target`" in prompt
    assert "This Agent Session was forked from `ses-source`." in prompt
    assert "The authoritative Avibe session id for this fork is `ses-target`." in prompt
    assert "use `ses-target` for Show Pages" in prompt


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
    assert captured["options"].disallowed_tools == ["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"]


def test_session_handler_ensures_agent_session_id_before_prompt(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class _PromptSessions(_Sessions):
        @staticmethod
        def ensure_agent_session_id(settings_key, agent_name, base_session_id):
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
    assert "--session-id sesk8m4q2p7x" in prompt
    assert "--session-key" not in prompt


def test_session_handler_preserves_passed_agent_system_prompt(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _PromptSessions(_Sessions):
        @staticmethod
        def ensure_agent_session_id(settings_key, agent_name, base_session_id):
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
        def ensure_agent_session_id(cls, settings_key, agent_name, base_session_id):
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
        def ensure_agent_session_id(settings_key, agent_name, base_session_id):
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
    controller.config.claude.default_model = "claude-sonnet-4-5"
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    first_client = _run_session(handler, context)
    second_client = _run_session(handler, context)

    assert first_client is second_client
    assert len(captured["clients"]) == 1
    assert captured["options"].extra_args == {"model": "claude-sonnet-4-5"}
    assert first_client.model_calls == []


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
    controller.config.claude.default_model = "claude-sonnet-4-5"
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    client = _run_session(handler, context)
    controller.config.claude.default_model = "claude-opus-4-1"

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

    assert captured["options"].extra_args == {}
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
        def ensure_agent_session_id(settings_key, agent_name, base_session_id):
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
        def ensure_agent_session_id(settings_key, agent_name, base_session_id):
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
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123")

    _run_session(handler, context)

    composite_key = f"slack_C123:{tmp_path}"
    handler.session_last_activity[composite_key] = 0.0

    evicted = asyncio.run(handler.evict_idle_sessions(600))

    assert evicted == 1
    assert captured["disconnects"] == 1
    assert composite_key not in controller.claude_sessions
    assert composite_key not in handler.session_last_activity


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
    assert captured["disconnects"] == 1
    assert composite_key not in controller.claude_sessions
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
        assert composite_key in handler.session_last_activity
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

    async def fake_reap(native_session_id, *, keep_pid=None, cli_path=None, logger, terminate_timeout=2.0):
        captured["reap_calls"].append((native_session_id, keep_pid, cli_path))
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
