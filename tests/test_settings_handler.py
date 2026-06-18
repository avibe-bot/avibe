from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from config.v2_settings import RoutingSettings
from core.handlers.settings_handler import SettingsHandler
from core.modals import RoutingModalSelection
from modules.im import MessageContext


class _StubSettingsManager:
    def __init__(self, routing: RoutingSettings | None):
        self.routing = routing
        self.saved_routing: RoutingSettings | None = None

    def get_channel_routing(self, settings_key: str) -> RoutingSettings | None:
        assert settings_key == "telegram::-100123"
        return self.routing

    def set_channel_routing(self, settings_key: str, routing: RoutingSettings) -> None:
        assert settings_key == "telegram::-100123"
        self.saved_routing = routing


def _make_handler(settings_manager: _StubSettingsManager) -> tuple[SettingsHandler, AsyncMock]:
    send_message = AsyncMock()
    controller = SimpleNamespace(
        config=SimpleNamespace(platform="telegram", language="en"),
        im_client=SimpleNamespace(send_message=send_message),
        settings_manager=settings_manager,
        _get_settings_key=lambda context: "telegram::-100123",
        _get_lang=lambda: "en",
    )
    return SettingsHandler(controller), send_message


def test_handle_routing_update_preserves_existing_codex_agent_when_omitted() -> None:
    settings_manager = _StubSettingsManager(
        RoutingSettings(
            agent_name="codex",
            codex_agent="reviewer",
            codex_model="gpt-5.4-mini",
            codex_reasoning_effort="low",
        )
    )
    handler, send_message = _make_handler(settings_manager)

    asyncio.run(
        handler.handle_routing_update(
            user_id="42",
            channel_id="-100123",
            backend="codex",
            opencode_agent=None,
            opencode_model=None,
            claude_agent=None,
            claude_model=None,
            codex_model="gpt-5.4",
            codex_reasoning_effort="high",
            notify_user=False,
            platform="telegram",
        )
    )

    assert settings_manager.saved_routing is not None
    assert settings_manager.saved_routing.codex_agent == "reviewer"
    assert settings_manager.saved_routing.model == "gpt-5.4"
    assert settings_manager.saved_routing.reasoning_effort == "high"
    assert settings_manager.saved_routing.codex_model is None
    assert settings_manager.saved_routing.codex_reasoning_effort is None
    send_message.assert_not_awaited()


def test_handle_routing_update_allows_explicit_codex_agent_clear() -> None:
    settings_manager = _StubSettingsManager(
        RoutingSettings(
            agent_name="codex",
            codex_agent="reviewer",
            codex_model="gpt-5.4-mini",
            codex_reasoning_effort="low",
        )
    )
    handler, _ = _make_handler(settings_manager)

    asyncio.run(
        handler.handle_routing_update(
            user_id="42",
            channel_id="-100123",
            backend="codex",
            opencode_agent=None,
            opencode_model=None,
            claude_agent=None,
            claude_model=None,
            codex_agent=None,
            codex_model="gpt-5.4",
            codex_reasoning_effort="high",
            notify_user=False,
            platform="telegram",
        )
    )

    assert settings_manager.saved_routing is not None
    assert settings_manager.saved_routing.codex_agent is None
    assert settings_manager.saved_routing.model == "gpt-5.4"
    assert settings_manager.saved_routing.reasoning_effort == "high"
    assert settings_manager.saved_routing.codex_model is None
    assert settings_manager.saved_routing.codex_reasoning_effort is None


def test_handle_routing_update_handles_first_codex_save_without_existing_routing() -> None:
    settings_manager = _StubSettingsManager(None)
    handler, send_message = _make_handler(settings_manager)

    asyncio.run(
        handler.handle_routing_update(
            user_id="42",
            channel_id="-100123",
            backend="codex",
            opencode_agent=None,
            opencode_model=None,
            claude_agent=None,
            claude_model=None,
            codex_model="gpt-5.4",
            codex_reasoning_effort="high",
            notify_user=False,
            platform="telegram",
        )
    )

    assert settings_manager.saved_routing is not None
    assert settings_manager.saved_routing.agent_name == "codex"
    assert settings_manager.saved_routing.codex_agent is None
    assert settings_manager.saved_routing.model == "gpt-5.4"
    assert settings_manager.saved_routing.reasoning_effort == "high"
    assert settings_manager.saved_routing.codex_model is None
    assert settings_manager.saved_routing.codex_reasoning_effort is None
    send_message.assert_not_awaited()


class _RoutingSettingsManager:
    def get_channel_routing(self, settings_key: str) -> RoutingSettings | None:
        assert settings_key == "slack::D0APS47LPU2"
        return None


class _FakeOpenCodeServer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ensure_running(self) -> None:
        self.calls.append("ensure_running")

    async def get_available_agents(self, directory: str) -> list[dict]:
        self.calls.append(f"agents:{directory}")
        return [{"name": "build"}]

    async def get_available_models(self, directory: str) -> dict:
        self.calls.append(f"models:{directory}")
        return {"providers": []}

    async def get_default_config(self, directory: str) -> dict:
        self.calls.append(f"config:{directory}")
        return {"model": "openai/gpt-5"}


class _FakeOpenCodeAgent:
    def __init__(self, server: _FakeOpenCodeServer) -> None:
        self.server = server

    async def _get_server(self) -> _FakeOpenCodeServer:
        return self.server


def _make_routing_handler() -> tuple[SettingsHandler, _FakeOpenCodeServer]:
    server = _FakeOpenCodeServer()
    controller = SimpleNamespace(
        config=SimpleNamespace(
            platform="slack",
            language="en",
            opencode=SimpleNamespace(enabled=True),
            claude=SimpleNamespace(enabled=True),
            codex=SimpleNamespace(enabled=True),
        ),
        im_client=SimpleNamespace(send_message=AsyncMock()),
        settings_manager=_RoutingSettingsManager(),
        _get_settings_key=lambda context: "slack::D0APS47LPU2",
        _get_lang=lambda: "en",
        resolve_agent_for_context=lambda context: "opencode",
        get_cwd=lambda context: "/tmp/workspace",
        agent_service=SimpleNamespace(
            agents={
                "opencode": _FakeOpenCodeAgent(server),
                "claude": object(),
                "codex": object(),
            }
        ),
    )
    return SettingsHandler(controller), server


def test_gather_routing_modal_data_only_fetches_current_backend() -> None:
    handler, server = _make_routing_handler()
    context = MessageContext(user_id="U1", channel_id="D0APS47LPU2", platform="slack")

    with patch("vibe.api.claude_models", side_effect=AssertionError("claude should not be fetched")), patch(
        "vibe.api.claude_agents", side_effect=AssertionError("claude should not be fetched")
    ), patch("vibe.api.codex_models", side_effect=AssertionError("codex should not be fetched")), patch(
        "vibe.api.codex_agents", side_effect=AssertionError("codex should not be fetched")
    ):
        data = asyncio.run(handler._gather_routing_modal_data(context))

    assert data.current_backend == "opencode"
    assert data.registered_backends == ["opencode", "claude", "codex"]
    assert server.calls == [
        "ensure_running",
        "agents:/tmp/workspace",
        "models:/tmp/workspace",
        "config:/tmp/workspace",
    ]


def test_gather_routing_modal_data_fetches_selected_backend_on_modal_update() -> None:
    handler, server = _make_routing_handler()
    context = MessageContext(user_id="U1", channel_id="D0APS47LPU2", platform="slack")

    with patch("vibe.api.claude_agents", return_value={"ok": True, "agents": [{"id": "reviewer"}]}), patch(
        "vibe.api.claude_models", return_value={"ok": True, "models": ["claude-sonnet-4-6"]}
    ), patch("vibe.api.codex_models", side_effect=AssertionError("codex should not be fetched")), patch(
        "vibe.api.codex_agents", side_effect=AssertionError("codex should not be fetched")
    ):
        data = asyncio.run(handler._gather_routing_modal_data(context, selected_backend="claude"))

    assert data.current_backend == "opencode"
    assert data.claude_agents == [{"id": "reviewer"}]
    assert data.claude_models == ["claude-sonnet-4-6"]
    assert server.calls == []


def test_gather_routing_modal_data_prefetches_all_backends_when_requested() -> None:
    handler, server = _make_routing_handler()
    context = MessageContext(user_id="U1", channel_id="D0APS47LPU2", platform="telegram")

    with patch("vibe.api.claude_agents", return_value={"ok": True, "agents": [{"id": "reviewer"}]}), patch(
        "vibe.api.claude_models", return_value={"ok": True, "models": ["claude-sonnet-4-6"]}
    ), patch("vibe.api.codex_agents", return_value={"ok": True, "agents": [{"id": "builder"}]}), patch(
        "vibe.api.codex_models", return_value={"ok": True, "models": ["gpt-5.4"]}
    ):
        data = asyncio.run(handler._gather_routing_modal_data(context, include_all_backend_data=True))

    assert data.registered_backends == ["opencode", "claude", "codex"]
    assert data.opencode_agents == [{"name": "build"}]
    assert data.claude_agents == [{"id": "reviewer"}]
    assert data.claude_models == ["claude-sonnet-4-6"]
    assert data.codex_agents == [{"id": "builder"}]
    assert data.codex_models == ["gpt-5.4"]
    assert server.calls == [
        "ensure_running",
        "agents:/tmp/workspace",
        "models:/tmp/workspace",
        "config:/tmp/workspace",
    ]


def test_gather_routing_modal_data_hides_disabled_backends() -> None:
    handler, server = _make_routing_handler()
    handler.config.claude.enabled = False
    handler.config.codex.enabled = False
    context = MessageContext(user_id="U1", channel_id="D0APS47LPU2", platform="slack")

    data = asyncio.run(handler._gather_routing_modal_data(context))

    assert data.registered_backends == ["opencode"]
    assert server.calls == [
        "ensure_running",
        "agents:/tmp/workspace",
        "models:/tmp/workspace",
        "config:/tmp/workspace",
    ]


def test_gather_routing_modal_data_falls_back_to_visible_backend_when_current_is_disabled() -> None:
    handler, server = _make_routing_handler()
    handler.config.claude.enabled = False
    context = MessageContext(user_id="U1", channel_id="D0APS47LPU2", platform="slack")
    handler.controller.resolve_agent_for_context = lambda context: "claude"

    with patch("vibe.api.claude_models", side_effect=AssertionError("claude should not be fetched")), patch(
        "vibe.api.claude_agents", side_effect=AssertionError("claude should not be fetched")
    ), patch("vibe.api.codex_models", side_effect=AssertionError("codex should not be fetched")), patch(
        "vibe.api.codex_agents", side_effect=AssertionError("codex should not be fetched")
    ):
        data = asyncio.run(handler._gather_routing_modal_data(context))

    assert data.current_backend == "opencode"
    assert data.registered_backends == ["opencode", "codex"]
    assert data.opencode_agents == [{"name": "build"}]
    assert server.calls == [
        "ensure_running",
        "agents:/tmp/workspace",
        "models:/tmp/workspace",
        "config:/tmp/workspace",
    ]


def test_handle_routing_modal_update_uses_visible_current_backend() -> None:
    handler, _server = _make_routing_handler()
    handler.config.claude.enabled = False
    handler.controller.resolve_agent_for_context = lambda context: "claude"
    update_calls = []

    async def _update_routing_modal(**kwargs):
        update_calls.append(kwargs)

    handler._get_im_client = lambda context: SimpleNamespace(update_routing_modal=_update_routing_modal)
    selection = RoutingModalSelection(selected_backend="opencode")

    asyncio.run(
        handler.handle_routing_modal_update(
            user_id="U1",
            channel_id="D0APS47LPU2",
            view_id="view-1",
            view_hash="hash-1",
            selection=selection,
            platform="slack",
        )
    )

    assert len(update_calls) == 1
    assert update_calls[0]["current_backend"] == "opencode"
    assert update_calls[0]["selected_backend"] == "opencode"
