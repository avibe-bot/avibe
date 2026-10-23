from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from config.v2_settings import RoutingSettings
from core.handlers.settings_handler import SettingsHandler
from core.modals import BackendRouteMenu, RouteOption, RoutingModalData, RoutingModalSelection
from modules.agents.catalog import AGENT_BACKENDS, NATIVE_CLI_BACKENDS
from modules.im import MessageContext


def _values(options) -> list[str]:
    return [option.value for option in options]


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


def test_settings_update_materializes_topic_mention_before_saving_other_fields() -> None:
    calls: list[str] = []
    user_settings = SimpleNamespace(show_message_types=[])

    class TopicSettingsManager:
        sessions = object()

        def set_require_mention(self, settings_key: str, value: bool | None) -> None:
            assert settings_key == "thread::-100123::1"
            assert value is None
            calls.append("mention")

        def get_user_settings(self, settings_key: str):
            assert settings_key == "thread::-100123::1"
            calls.append("read")
            return user_settings

        def update_user_settings(self, settings_key: str, settings) -> None:
            assert settings_key == "thread::-100123::1"
            assert settings.show_message_types == ["assistant"]
            calls.append("save")

    manager = TopicSettingsManager()
    controller = SimpleNamespace(
        config=SimpleNamespace(platform="telegram", language="en"),
        im_client=SimpleNamespace(send_message=AsyncMock()),
        settings_manager=manager,
        _get_settings_key=lambda _context: "thread::-100123::1",
        _get_lang=lambda: "en",
    )
    handler = SettingsHandler(controller)

    asyncio.run(
        handler.handle_settings_update(
            user_id="42",
            channel_id="-100123",
            thread_id="1",
            show_message_types=["assistant"],
            require_mention=None,
            notify_user=False,
            platform="telegram",
        )
    )

    assert calls == ["mention", "read", "save"]


class _FlatScopeSessions:
    def __init__(self, row: dict | None):
        self.row = row
        self.calls: list[tuple[str, str]] = []

    def find_session_for_anchor(self, session_key: str, session_anchor: str):
        self.calls.append((session_key, session_anchor))
        return self.row


class _FlatScopeSettingsManager:
    def __init__(self, routing: RoutingSettings | None = None):
        self.routing = routing
        self.saved_routing: RoutingSettings | None = None

    def get_channel_routing(self, settings_key: str) -> RoutingSettings | None:
        assert settings_key == "58181121"
        return self.routing

    def set_channel_routing(self, settings_key: str, routing: RoutingSettings) -> None:
        assert settings_key == "58181121"
        self.saved_routing = routing


def _make_flat_scope_handler(
    *,
    platform: str = "telegram",
    row: dict | None,
    threaded: bool = False,
) -> tuple[SettingsHandler, AsyncMock, _FlatScopeSessions]:
    send_message = AsyncMock()
    sessions = _FlatScopeSessions(row)
    settings_manager = _FlatScopeSettingsManager(RoutingSettings(agent_name="opencode"))
    im_client = SimpleNamespace(
        send_message=send_message,
        should_use_thread_for_reply=lambda: threaded,
        should_use_thread_for_dm_session=lambda: threaded,
        should_use_message_id_for_channel_session=lambda context=None: threaded,
    )
    controller = SimpleNamespace(
        config=SimpleNamespace(platform=platform, language="zh"),
        im_client=im_client,
        settings_manager=settings_manager,
        sessions=sessions,
        session_handler=SimpleNamespace(get_base_session_id=lambda context: f"{platform}_58181121"),
        _get_settings_key=lambda context: "58181121",
        _get_session_key=lambda context: f"{platform}::user::58181121",
        _get_lang=lambda: "zh",
    )
    return SettingsHandler(controller), send_message, sessions


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
            model="gpt-5.4",
            reasoning_effort="high",
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
            variant=None,
            model="gpt-5.4",
            reasoning_effort="high",
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
            model="gpt-5.4",
            reasoning_effort="high",
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


def test_handle_routing_update_preserves_catalog_claude_effort() -> None:
    settings_manager = _StubSettingsManager(None)
    handler, send_message = _make_handler(settings_manager)

    with patch(
        "core.handlers.settings_handler.backend_model_catalog.catalog_reasoning_efforts_for_model",
        return_value=["low", "ultra"],
    ):
        asyncio.run(
            handler.handle_routing_update(
                user_id="42",
                channel_id="-100123",
                backend="claude",
                variant="reviewer",
                model="claude-future-6",
                reasoning_effort="ultra",
                notify_user=False,
                platform="telegram",
            )
        )

    assert settings_manager.saved_routing is not None
    assert settings_manager.saved_routing.model == "claude-future-6"
    assert settings_manager.saved_routing.reasoning_effort == "ultra"
    send_message.assert_not_awaited()


def test_handle_routing_update_saves_vibey_model_and_effort_on_the_vibey_route() -> None:
    settings_manager = _StubSettingsManager(RoutingSettings(agent_name="opencode", model="openai/gpt-5"))
    handler, send_message = _make_handler(settings_manager)
    handler.controller.vibe_agent_store = SimpleNamespace(routing_name_for_backend=lambda backend: f"{backend}-agent")

    asyncio.run(
        handler.handle_routing_update(
            user_id="42",
            channel_id="-100123",
            backend="vibey",
            model="claude-opus-5-5",
            reasoning_effort="xhigh",
            platform="telegram",
        )
    )

    saved = settings_manager.saved_routing
    assert (saved.agent_name, saved.model, saved.reasoning_effort) == ("vibey-agent", "claude-opus-5-5", "xhigh")
    text = send_message.await_args.args[1]
    assert "claude-opus-5-5" in text and "xhigh" in text


def test_saving_an_unchanged_slack_form_keeps_a_stored_model_slack_cannot_show() -> None:
    """A Web-set model id past Slack's 150-character option values must survive saving the Slack form."""
    from config.v2_config import SlackConfig
    from modules.im.slack import SlackBot

    long_id = "vendor/" + "x" * 160
    settings_manager = _StubSettingsManager(RoutingSettings(agent_name="vibey", model=long_id, reasoning_effort="high"))
    handler, _ = _make_handler(settings_manager)
    handler.controller.vibe_agent_store = SimpleNamespace(routing_name_for_backend=lambda backend: backend)
    bot = SlackBot(SlackConfig(bot_token="fixture-token"))
    bot._on_routing_update = handler.handle_routing_update
    data = RoutingModalData(["vibey"], "vibey", settings_manager.routing)
    data.menus["vibey"] = BackendRouteMenu(
        "vibey",
        models=[RouteOption("model-a", "Model A")],
        efforts={"": [RouteOption("high", "High")]},
        option_limit=bot.route_option_limit,
        value_limit=bot.route_option_value_limit,
        stored=data.stored_draft("vibey"),
    )
    view = bot._build_routing_modal_view(channel_id="-100123", routing=data)
    view["state"] = {"values": {
        block["block_id"]: {block["element"]["action_id"]: {"selected_option": block["element"]["initial_option"]}}
        for block in view["blocks"] if block["type"] == "input"
    }}

    with patch.object(handler, "_routing_update_needs_new_session_hint", return_value=False):
        asyncio.run(bot._handle_view_submission({"view": view, "user": {"id": "42"}}))

    saved = settings_manager.saved_routing
    assert (saved.agent_name, saved.model, saved.reasoning_effort) == ("vibey", long_id, "high")


def test_handle_routing_update_warns_flat_scope_with_existing_backend_session() -> None:
    handler, send_message, sessions = _make_flat_scope_handler(row={"agent_backend": "opencode"})

    asyncio.run(
        handler.handle_routing_update(
            user_id="58181121",
            channel_id="58181121",
            backend="claude",
            variant=None,
            model=None,
            platform="telegram",
            is_dm=True,
        )
    )

    text = send_message.await_args.args[1]
    assert "Agent设置已更新！" in text
    assert "请使用 /new 命令创建新会话，以使设置变更生效。新会话创建后将覆盖当前会话。" in text
    assert sessions.calls == [("telegram::user::58181121", "telegram_58181121")]


def test_handle_routing_update_warns_wechat_flat_scope_with_existing_backend_session() -> None:
    handler, send_message, sessions = _make_flat_scope_handler(platform="wechat", row={"agent_backend": "opencode"})

    asyncio.run(
        handler.handle_routing_update(
            user_id="58181121",
            channel_id="58181121",
            backend="claude",
            variant=None,
            model=None,
            platform="wechat",
            is_dm=True,
        )
    )

    text = send_message.await_args.args[1]
    assert "请使用 /new 命令创建新会话，以使设置变更生效。新会话创建后将覆盖当前会话。" in text
    assert sessions.calls == [("wechat::user::58181121", "wechat_58181121")]


def test_handle_routing_update_does_not_warn_flat_scope_same_backend_model_change() -> None:
    handler, send_message, sessions = _make_flat_scope_handler(
        row={
            "agent_name": "claude",
            "agent_backend": "claude",
            "agent_variant": "claude",
            "model": "claude-sonnet-4-5",
            "reasoning_effort": "",
        }
    )

    asyncio.run(
        handler.handle_routing_update(
            user_id="58181121",
            channel_id="58181121",
            backend="claude",
            variant=None,
            model="claude-opus-4-1",
            platform="telegram",
            is_dm=True,
        )
    )

    text = send_message.await_args.args[1]
    assert "请使用 /new 命令创建新会话" not in text
    assert sessions.calls == [("telegram::user::58181121", "telegram_58181121")]


def test_handle_routing_update_does_not_warn_flat_scope_same_backend_reasoning_change() -> None:
    handler, send_message, sessions = _make_flat_scope_handler(
        row={
            "agent_name": "claude",
            "agent_backend": "claude",
            "agent_variant": "claude",
            "model": "claude-sonnet-4-5",
            "reasoning_effort": "low",
        }
    )

    asyncio.run(
        handler.handle_routing_update(
            user_id="58181121",
            channel_id="58181121",
            backend="claude",
            variant=None,
            model="claude-sonnet-4-5",
            reasoning_effort="high",
            platform="telegram",
            is_dm=True,
        )
    )

    text = send_message.await_args.args[1]
    assert "请使用 /new 命令创建新会话" not in text
    assert sessions.calls == [("telegram::user::58181121", "telegram_58181121")]


def test_handle_routing_update_does_not_warn_flat_scope_same_session_target() -> None:
    handler, send_message, sessions = _make_flat_scope_handler(
        row={
            "agent_name": "claude",
            "agent_backend": "claude",
            "agent_variant": "claude",
            "model": "claude-sonnet-4-5",
            "reasoning_effort": "",
        }
    )

    asyncio.run(
        handler.handle_routing_update(
            user_id="58181121",
            channel_id="58181121",
            backend="claude",
            variant=None,
            model="claude-sonnet-4-5",
            platform="telegram",
            is_dm=True,
        )
    )

    text = send_message.await_args.args[1]
    assert "请使用 /new 命令创建新会话" not in text
    assert sessions.calls == [("telegram::user::58181121", "telegram_58181121")]


def test_handle_routing_update_does_not_warn_when_flat_scope_has_no_session() -> None:
    handler, send_message, sessions = _make_flat_scope_handler(row=None)

    asyncio.run(
        handler.handle_routing_update(
            user_id="58181121",
            channel_id="58181121",
            backend="claude",
            variant=None,
            model=None,
            platform="telegram",
            is_dm=True,
        )
    )

    text = send_message.await_args.args[1]
    assert "Agent设置已更新！" in text
    assert "请使用 /new 命令创建新会话" not in text
    assert sessions.calls == [("telegram::user::58181121", "telegram_58181121")]


def test_handle_routing_update_does_not_warn_threaded_scope() -> None:
    handler, send_message, sessions = _make_flat_scope_handler(platform="slack", row={"agent_backend": "opencode"}, threaded=True)

    asyncio.run(
        handler.handle_routing_update(
            user_id="U123",
            channel_id="D123",
            backend="claude",
            variant=None,
            model=None,
            platform="slack",
            is_dm=True,
        )
    )

    text = send_message.await_args.args[1]
    assert "Agent设置已更新！" in text
    assert "请使用 /new 命令创建新会话" not in text
    assert sessions.calls == []


class _RoutingSettingsManager:
    def get_channel_routing(self, settings_key: str) -> RoutingSettings | None:
        assert settings_key == "slack::D0APS47LPU2"
        return None


class _FakeOpenCodeServer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.model_hub_models: list[dict | None] = []

    async def get_available_agents(self, directory: str) -> list[dict]:
        self.calls.append(f"agents:{directory}")
        return [{"name": "build"}]

    async def get_available_models(
        self,
        directory: str,
        *,
        model_hub_models: dict | None = None,
    ) -> dict:
        self.calls.append(f"models:{directory}")
        self.model_hub_models.append(model_hub_models)
        return {"providers": []}

    async def get_default_config(self, directory: str) -> dict:
        self.calls.append(f"config:{directory}")
        return {"model": "openai/gpt-5"}


class _FakeOpenCodeAgent:
    def __init__(self, server: _FakeOpenCodeServer) -> None:
        self.server = server

    @asynccontextmanager
    async def current_server(self):
        self.server.calls.append("current_server")
        yield self.server


def _make_routing_handler() -> tuple[SettingsHandler, _FakeOpenCodeServer]:
    server = _FakeOpenCodeServer()
    controller = SimpleNamespace(
        config=SimpleNamespace(
            platform="slack",
            language="en",
            **{backend: SimpleNamespace(enabled=True) for backend in NATIVE_CLI_BACKENDS},
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
                **{backend: object() for backend in AGENT_BACKENDS if backend != "opencode"},
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
    assert data.registered_backends == list(AGENT_BACKENDS)  # Catalog order: the built-in Avibe Agent first.
    assert server.calls == [
        "current_server",
        "agents:/tmp/workspace",
        "models:/tmp/workspace",
        "config:/tmp/workspace",
    ]


def test_gather_routing_modal_data_uses_current_model_hub_projection() -> None:
    handler, server = _make_routing_handler()
    projection = {
        "current-model": {
            "id": "current-model",
            "native_protocol": "openai_responses",
        },
    }
    handler.controller.model_hub_service = SimpleNamespace(
        opencode_public_models=lambda: projection,
    )
    context = MessageContext(
        user_id="U1",
        channel_id="D0APS47LPU2",
        platform="slack",
    )

    asyncio.run(handler._gather_routing_modal_data(context))

    assert server.model_hub_models == [projection]


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
    assert _values(data.menus["claude"].variants) == ["reviewer"]
    assert _values(data.menus["claude"].models) == ["claude-sonnet-4-6"]
    assert server.calls == []


def test_gather_routing_modal_data_prefetches_all_backends_when_requested() -> None:
    handler, server = _make_routing_handler()
    context = MessageContext(user_id="U1", channel_id="D0APS47LPU2", platform="telegram")

    with patch("vibe.api.claude_agents", return_value={"ok": True, "agents": [{"id": "reviewer"}]}), patch(
        "vibe.api.claude_models",
        return_value={
            "ok": True,
            # claude_models() appends the bare aliases after the dated catalog.
            "models": ["claude-sonnet-4-6", "opus"],
            "reasoning_options": {"claude-sonnet-4-6": [{"value": "max", "label": "Max"}]},
        },
    ), patch("vibe.api.codex_agents", return_value={"ok": True, "agents": [{"id": "builder"}]}), patch(
        "vibe.api.codex_models",
        return_value={
            "ok": True,
            "models": ["gpt-5.4"],
            "reasoning_options": {"gpt-5.4": [{"value": "ultra", "label": "Ultra"}]},
        },
    ):
        data = asyncio.run(handler._gather_routing_modal_data(context, include_all_backend_data=True))

    assert data.registered_backends == list(AGENT_BACKENDS)
    menus = data.menus
    assert {backend: _values(menu.variants) for backend, menu in menus.items()} == {
        "vibey": [], "opencode": ["build"], "claude": ["reviewer"], "codex": ["builder"],
    }
    # The always-valid bare aliases lead, so a capped select trims dated snapshots first.
    assert _values(menus["claude"].models) == ["opus", "claude-sonnet-4-6"]
    assert _values(menus["codex"].models) == ["gpt-5.4"]
    # Each model's catalog efforts, not a backend-wide ladder.
    assert _values(menus["claude"].efforts["claude-sonnet-4-6"]) == ["max"]
    assert _values(menus["codex"].efforts["gpt-5.4"]) == ["ultra"]
    assert server.calls == [
        "current_server",
        "agents:/tmp/workspace",
        "models:/tmp/workspace",
        "config:/tmp/workspace",
    ]


def test_gather_routing_modal_data_reads_vibey_menu_from_model_hub() -> None:
    handler, server = _make_routing_handler()
    handler.controller.resolve_agent_for_context = lambda context: "vibey"
    catalog = [
        {"id": "model-a", "display_name": "Model A", "routeable": True, "supports_reasoning": True,
         "reasoning_efforts": ["low", "high", "max"], "effort_menu": {"omitted": ["max"], "not_on": {}}},
        {"id": "model-b", "display_name": None, "routeable": True, "supports_reasoning": False,
         "reasoning_efforts": ["low"], "effort_menu": {"omitted": [], "not_on": {}}},
        {"id": "selector", "routeable": False, "reasoning_efforts": []},
    ]
    read_agent_sources = AsyncMock(return_value={"backend": "vibey", "catalog_models": catalog})
    handler.controller.model_hub_service = SimpleNamespace(read_agent_sources=read_agent_sources)
    handler.controller.vibe_agent_store = SimpleNamespace(
        get_builtin_default_agent_for_backend=lambda backend, enabled_only=True: SimpleNamespace(model="model-a"),
    )
    context = MessageContext(user_id="U1", channel_id="D0APS47LPU2", platform="slack")

    data = asyncio.run(handler._gather_routing_modal_data(context))

    read_agent_sources.assert_awaited_once_with("vibey")
    assert data.current_backend == "vibey"
    menu = data.menus["vibey"]
    assert menu.models == [RouteOption("model-a", "Model A"), RouteOption("model-b", "model-b")]
    # Model Hub's omitted efforts are not offered; a route without its own model inherits the Vibey Agent's.
    assert {model: _values(efforts) for model, efforts in menu.efforts.items()} == {
        "model-a": ["low", "high"],
        "model-b": [],
        "": ["low", "high"],
    }
    assert server.calls == []


def test_gather_routing_modal_data_hides_disabled_backends() -> None:
    handler, server = _make_routing_handler()
    handler.config.claude.enabled = False
    handler.config.codex.enabled = False
    context = MessageContext(user_id="U1", channel_id="D0APS47LPU2", platform="slack")

    data = asyncio.run(handler._gather_routing_modal_data(context))

    assert data.registered_backends == ["vibey", "opencode"]
    assert server.calls == [
        "current_server",
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

    # Listed first, the built-in backend is still not the fallback while another is enabled.
    assert data.current_backend == "opencode"
    assert data.registered_backends == ["vibey", "opencode", "codex"]
    assert _values(data.menus["opencode"].variants) == ["build"]
    assert server.calls == [
        "current_server",
        "agents:/tmp/workspace",
        "models:/tmp/workspace",
        "config:/tmp/workspace",
    ]


def test_slack_backend_switch_back_shows_the_stored_route_not_blanks() -> None:
    """Switching the Slack modal away from the stored backend and back must not blank what saving would persist."""
    from config.v2_config import SlackConfig
    from modules.im.slack import SlackBot
    from modules.im.slack_modal import parse_routing_modal_selection

    handler, _server = _make_routing_handler()
    routing = RoutingSettings(agent_name="vibey", model="model-b", reasoning_effort="high")
    handler.settings_manager = SimpleNamespace(get_channel_routing=lambda settings_key: routing)
    handler.controller.resolve_agent_for_context = lambda context: "vibey"
    catalog = [
        {"id": model_id, "routeable": True, "reasoning_efforts": ["low", "high"], "effort_menu": {"omitted": []}}
        for model_id in ("model-a", "model-b")
    ]
    handler.controller.model_hub_service = SimpleNamespace(
        read_agent_sources=AsyncMock(return_value={"catalog_models": catalog}),
    )
    bot = SlackBot(SlackConfig(bot_token="fixture-token"))
    bot._ensure_clients = lambda: None
    bot.web_client = SimpleNamespace(views_update=AsyncMock())
    handler._get_im_client = lambda context: bot
    # The modal as Slack holds it after a switch to OpenCode: it has no Vibey controls.
    view = bot._build_routing_modal_view(
        channel_id="D0APS47LPU2",
        routing=RoutingModalData(registered_backends=["vibey", "opencode"], current_backend="vibey", current_routing=routing),
        selected_backend="opencode",
    )
    view["state"] = {"values": {
        block["block_id"]: {block["element"]["action_id"]: {"selected_option": block["element"]["initial_option"]}}
        for block in view["blocks"] if block["type"] == "input"
    }}
    view["state"]["values"]["backend_block"]["backend_select"] = {"selected_option": {"value": "vibey"}}
    selection = parse_routing_modal_selection(
        view=view, action={"action_id": "backend_select", "selected_option": {"value": "vibey"}}
    )

    asyncio.run(
        handler.handle_routing_modal_update(
            user_id="U1", channel_id="D0APS47LPU2", view_id="view-1", view_hash="hash-1",
            selection=selection, platform="slack",
        )
    )

    updated = bot.web_client.views_update.await_args.kwargs["view"]
    shown = {
        block["block_id"].rsplit(":", 1)[0] if block["block_id"].startswith("route_effort:") else block["block_id"]:
        block["element"]["initial_option"]["value"]
        for block in updated["blocks"] if block["type"] == "input"
    }
    assert (shown["route_model:vibey"], shown["route_effort:vibey"]) == ("model-b", "high")


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
    assert update_calls[0]["routing"].current_backend == "opencode"
    assert update_calls[0]["selected_backend"] == "opencode"
