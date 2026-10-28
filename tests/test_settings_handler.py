from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from config.v2_settings import RoutingSettings
from core.handlers.settings_handler import SettingsHandler
from core.modals import (
    STORED_OPTION_VALUE,
    AgentChoice,
    BackendRouteMenu,
    RouteOption,
    RoutingModalData,
    RoutingModalSelection,
)
from modules.agents.catalog import AGENT_BACKENDS, NATIVE_CLI_BACKENDS
from modules.im import MessageContext


def _values(options) -> list[str]:
    return [option.value for option in options]


class _AgentStore:
    """The built-in Agent of every backend, as ``VibeAgentStore`` returns them (id ``id-<backend>``)."""

    def __init__(self, *extra):
        self.agents = [
            SimpleNamespace(id=f"id-{backend}", name=backend, backend=backend, model=None, reasoning_effort=None,
                            enabled=True)
            for backend in AGENT_BACKENDS
        ] + list(extra)

    def list_agents(self, *, include_disabled=True, **_kwargs):
        return [agent for agent in self.agents if include_disabled or agent.enabled]

    def require_reference_by_id(self, agent_id):
        return next(agent for agent in self.agents if agent.id == agent_id)

    def get(self, name):
        return next((agent for agent in self.agents if agent.name == name), None)


class _StubSettingsManager:
    def __init__(self, routing: RoutingSettings | None):
        self.routing = routing
        self.saved_routing: RoutingSettings | None = None

    def get_channel_routing(self, settings_key: str) -> RoutingSettings | None:
        assert settings_key == "telegram::-100123"
        return self.routing

    def set_channel_routing(self, settings_key: str, routing: RoutingSettings) -> RoutingSettings:
        assert settings_key == "telegram::-100123"
        self.saved_routing = routing
        return routing


def _make_handler(settings_manager: _StubSettingsManager) -> tuple[SettingsHandler, AsyncMock]:
    send_message = AsyncMock()
    controller = SimpleNamespace(
        config=SimpleNamespace(platform="telegram", language="en"),
        im_client=SimpleNamespace(send_message=send_message),
        settings_manager=settings_manager,
        vibe_agent_store=_AgentStore(),
        _get_settings_key=lambda context: "telegram::-100123",
        _get_lang=lambda: "en",
    )
    return SettingsHandler(controller), send_message


def test_settings_update_saves_a_topic_override_in_one_write(tmp_path, monkeypatch) -> None:
    """A topic's first settings form saves its types and the mention gate it inherited, together."""
    from config import paths
    from config.v2_settings import ChannelSettings, SettingsStore, make_thread_settings_key
    from modules.settings_manager import SettingsManager

    monkeypatch.setattr(paths, "ensure_data_dirs", lambda: None)
    SettingsStore.reset_instance()
    manager = SettingsManager(settings_file=str(tmp_path / "settings.json"), platform="telegram")
    manager.require_mention_default = lambda: False
    try:
        manager.store.update_channel(
            "-100123",
            ChannelSettings(enabled=True, show_message_types=["assistant", "toolcall"], require_mention=True),
            platform="telegram",
        )
        writes = []
        save_state = manager.store._service.save_state
        monkeypatch.setattr(
            manager.store._service, "save_state", lambda *args, **kwargs: writes.append(1) or save_state(*args, **kwargs)
        )
        controller = SimpleNamespace(
            config=SimpleNamespace(platform="telegram", language="en"),
            im_client=SimpleNamespace(send_message=AsyncMock()),
            settings_manager=manager,
            _get_settings_key=lambda _context: make_thread_settings_key("-100123", "1"),
            _get_lang=lambda: "en",
        )

        asyncio.run(
            SettingsHandler(controller).handle_settings_update(
                user_id="42",
                channel_id="-100123",
                thread_id="1",
                show_message_types=["assistant"],
                require_mention=None,
                notify_user=False,
                platform="telegram",
            )
        )

        topic = manager.store.find_thread("-100123", "1", platform="telegram")
        # The form's "default" gate on a new topic keeps the channel's explicit gate.
        assert (topic.show_message_types, topic.require_mention, topic.enabled) == (["assistant"], True, True)
        assert manager.store.find_channel("-100123", platform="telegram").show_message_types == ["assistant", "toolcall"]
        assert len(writes) == 1
    finally:
        SettingsStore.reset_instance()


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

    def set_channel_routing(self, settings_key: str, routing: RoutingSettings) -> RoutingSettings:
        assert settings_key == "58181121"
        self.saved_routing = routing
        return routing


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
        vibe_agent_store=_AgentStore(),
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
            agent_id="id-codex",
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
            agent_id="id-codex",
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
            agent_id="id-codex",
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


def test_handle_routing_update_routes_to_the_chosen_agent_with_its_overrides() -> None:
    settings_manager = _StubSettingsManager(
        RoutingSettings(agent_name="opencode", model="openai/gpt-5", opencode_agent="build", claude_agent="helper")
    )
    handler, send_message = _make_handler(settings_manager)
    # A user Agent on a backend whose built-in Agent is also listed: the route names this one, not the built-in.
    handler.controller.vibe_agent_store.agents.append(
        SimpleNamespace(id="id-pm", name="pm-claude", backend="claude", model="claude-opus-5-5", enabled=True)
    )

    asyncio.run(
        handler.handle_routing_update(
            user_id="42",
            channel_id="-100123",
            agent_id="id-pm",
            model="claude-sonnet-5-5",
            reasoning_effort="high",
            platform="telegram",
        )
    )

    saved = settings_manager.saved_routing
    assert (saved.agent_name, saved.model, saved.reasoning_effort) == ("pm-claude", "claude-sonnet-5-5", "high")
    # Native agents stay per backend, the chosen Agent's included, since the form omitted it.
    assert (saved.opencode_agent, saved.claude_agent) == ("build", "helper")
    text = send_message.await_args.args[1]
    assert "pm-claude" in text and "claude-sonnet-5-5" in text

    # An opaque id of any length still yields a confirmation every platform can send.
    asyncio.run(
        handler.handle_routing_update(
            user_id="42", channel_id="-100123", agent_id="id-vibey", model="m" * 5000, platform="telegram",
        )
    )
    assert settings_manager.saved_routing.model == "m" * 5000
    assert len(send_message.await_args.args[1]) < 1000


def test_saving_an_unchanged_slack_form_keeps_a_stored_model_slack_cannot_show() -> None:
    """A Web-set model id past Slack's 150-character option values must survive saving the Slack form."""
    from config.v2_config import SlackConfig
    from modules.im.slack import SlackBot

    long_id = "vendor/" + "x" * 160
    settings_manager = _StubSettingsManager(RoutingSettings(agent_name="vibey", model=long_id, reasoning_effort="high"))
    handler, _ = _make_handler(settings_manager)
    bot = SlackBot(SlackConfig(bot_token="fixture-token"))
    bot._on_routing_update = handler.handle_routing_update
    data = RoutingModalData([AgentChoice("id-vibey", "vibey", "vibey")], "id-vibey", settings_manager.routing)
    data.menus["vibey"] = BackendRouteMenu(
        "vibey",
        models=[RouteOption("model-a", "Model A")],
        efforts={"": [RouteOption("high", "High")]},
        option_limit=bot.route_option_limit,
        value_limit=bot.route_option_value_limit,
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


def test_a_stored_stand_in_resolves_only_against_a_stored_route_on_its_agent() -> None:
    # The form rendered while the route was on Vibey; by the time it saves, the route may be on Codex.
    for stored_agent, expected in (("vibey", ("long-vibey-model", "high")), ("codex", (None, None))):
        stored = RoutingSettings(agent_name=stored_agent, model=f"long-{stored_agent}-model", reasoning_effort="high")
        settings_manager = _StubSettingsManager(stored)
        handler, _ = _make_handler(settings_manager)

        asyncio.run(
            handler.handle_routing_update(
                user_id="42", channel_id="-100123", agent_id="id-vibey",
                model=STORED_OPTION_VALUE, reasoning_effort=STORED_OPTION_VALUE,
                notify_user=False, platform="telegram",
            )
        )

        saved = settings_manager.saved_routing
        assert (saved.agent_name, saved.model, saved.reasoning_effort) == ("vibey", *expected)


def test_handle_routing_update_warns_flat_scope_with_existing_backend_session() -> None:
    handler, send_message, sessions = _make_flat_scope_handler(row={"agent_backend": "opencode"})

    asyncio.run(
        handler.handle_routing_update(
            user_id="58181121",
            channel_id="58181121",
            agent_id="id-claude",
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
            agent_id="id-claude",
            variant=None,
            model=None,
            platform="wechat",
            is_dm=True,
        )
    )

    text = send_message.await_args.args[1]
    assert "请使用 /new 命令创建新会话，以使设置变更生效。新会话创建后将覆盖当前会话。" in text
    assert sessions.calls == [("wechat::user::58181121", "wechat_58181121")]


def test_handle_routing_update_warns_flat_scope_switching_to_another_agent_on_the_same_backend() -> None:
    handler, send_message, _sessions = _make_flat_scope_handler(
        row={"agent_name": "claude", "agent_backend": "claude", "agent_variant": "claude", "model": "", "reasoning_effort": ""}
    )
    handler.controller.vibe_agent_store.agents.append(
        SimpleNamespace(id="id-pm", name="pm-claude", backend="claude", model=None, reasoning_effort=None, enabled=True)
    )

    asyncio.run(
        handler.handle_routing_update(
            user_id="58181121", channel_id="58181121", agent_id="id-pm", platform="telegram", is_dm=True,
        )
    )

    # The scope's session keeps the Agent it started with, so the new one needs /new.
    assert "请使用 /new 命令创建新会话" in send_message.await_args.args[1]


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
            agent_id="id-claude",
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
            agent_id="id-claude",
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
            agent_id="id-claude",
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
            agent_id="id-claude",
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
            agent_id="id-claude",
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


def _make_routing_handler(current: str = "opencode") -> tuple[SettingsHandler, _FakeOpenCodeServer]:
    server = _FakeOpenCodeServer()
    store = _AgentStore()
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
        vibe_agent_store=store,
        # The Agent the scope runs now; tests point it elsewhere through ``controller.current``.
        current=current,
        get_cwd=lambda context: "/tmp/workspace",
        agent_service=SimpleNamespace(
            agents={
                "opencode": _FakeOpenCodeAgent(server),
                **{backend: object() for backend in AGENT_BACKENDS if backend != "opencode"},
            }
        ),
    )
    controller.resolve_vibe_agent_for_context = lambda context, required=True: store.get(controller.current)
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

    assert data.current_agent == "id-opencode"
    # Every enabled Agent on a runnable backend, as the Web route picker lists them.
    assert [agent.id for agent in data.agents] == [f"id-{backend}" for backend in AGENT_BACKENDS]
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
        data = asyncio.run(handler._gather_routing_modal_data(context, selected_agent="id-claude"))

    assert data.current_agent == "id-opencode"
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


def test_opencode_route_menu_fits_the_platforms_cap_and_is_whole_without_one() -> None:
    handler, server = _make_routing_handler()
    # More providers than the select has slots, each leading (newest first) with ids too long for Discord.
    providers = [
        {
            "id": f"p{p}",
            "name": f"P{p}",
            "models": {
                **{f"{'x' * 120}-{i}": {"name": f"Long {i}", "release_date": "2026-09-01"} for i in range(6)},
                **{f"m{i}": {"name": f"M{i}", "release_date": "2026-01-01"} for i in range(3)},
            },
        }
        for p in range(30)
    ]
    # An OpenCode Agent whose own model sits past Discord's cap, with efforts of its own.
    providers[29]["models"]["m2"]["variants"] = {"max": {}}
    handler.controller.vibe_agent_store.agents.append(
        SimpleNamespace(id="id-planner", name="planner", backend="opencode", model="p29/m2", reasoning_effort=None,
                        enabled=True)
    )
    server.get_available_models = AsyncMock(return_value={"providers": providers})
    context = MessageContext(user_id="U1", channel_id="D0APS47LPU2", platform="discord")
    menus = {}
    for limit, value_limit in ((25, 100), (None, None)):  # Discord's select; Telegram's paged picker
        handler._get_im_client = lambda context, limit=limit, value_limit=value_limit: SimpleNamespace(
            route_option_limit=limit, route_option_value_limit=value_limit
        )
        menus[limit] = asyncio.run(handler._gather_routing_modal_data(context)).menus["opencode"]

    capped = menus[25].model_options(None)
    assert len(capped) == 24 and all("/m" in option.value for option in capped)
    assert "p29/m2" not in {option.value for option in capped}
    # Its Default still offers that model's efforts, not a generic ladder.
    planner = RoutingModalData(
        agents=[AgentChoice("id-planner", "planner", "opencode", model="p29/m2")], current_agent="id-planner",
        current_routing=None, menus={"opencode": menus[25]},
    )
    assert _values(planner.menu("id-planner").effort_options(None, None)) == ["max"]
    assert len(menus[None].model_options(None)) == 270


def test_gather_routing_modal_data_reads_vibey_menu_from_model_hub() -> None:
    handler, server = _make_routing_handler()
    handler.controller.current = "vibey"
    catalog = [
        {"id": "model-a", "display_name": "Model A", "routeable": True, "supports_reasoning": True,
         "reasoning_efforts": ["low", "high", "max"], "effort_menu": {"omitted": ["max"], "not_on": {}}},
        {"id": "model-b", "display_name": None, "routeable": True, "supports_reasoning": False,
         "reasoning_efforts": ["low"], "effort_menu": {"omitted": [], "not_on": {}}},
        {"id": "selector", "routeable": False, "reasoning_efforts": []},
    ]
    read_agent_sources = AsyncMock(return_value={"backend": "vibey", "catalog_models": catalog})
    handler.controller.model_hub_service = SimpleNamespace(read_agent_sources=read_agent_sources)
    # The platform's declared select caps size every menu and the Agent select.
    handler._get_im_client = lambda context: SimpleNamespace(route_option_limit=25, route_option_value_limit=100)
    context = MessageContext(user_id="U1", channel_id="D0APS47LPU2", platform="discord")

    data = asyncio.run(handler._gather_routing_modal_data(context))

    read_agent_sources.assert_awaited_once_with("vibey")
    assert data.current_agent == "id-vibey"
    menu = data.menus["vibey"]
    assert menu.models == [RouteOption("model-a", "Model A"), RouteOption("model-b", "model-b")]
    # Model Hub's omitted efforts are not offered.
    assert {model: _values(efforts) for model, efforts in menu.efforts.items()} == {
        "model-a": ["low", "high"],
        "model-b": [],
    }
    assert (menu.option_limit, menu.value_limit, data.agent_option_limit) == (25, 100, 25)
    assert server.calls == []


def test_gather_routing_modal_data_hides_disabled_backends() -> None:
    handler, server = _make_routing_handler()
    handler.config.claude.enabled = False
    handler.config.codex.enabled = False
    context = MessageContext(user_id="U1", channel_id="D0APS47LPU2", platform="slack")

    data = asyncio.run(handler._gather_routing_modal_data(context))

    assert [agent.id for agent in data.agents] == ["id-vibey", "id-opencode"]
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
    handler.controller.current = "claude"

    with patch("vibe.api.claude_models", side_effect=AssertionError("claude should not be fetched")), patch(
        "vibe.api.claude_agents", side_effect=AssertionError("claude should not be fetched")
    ), patch("vibe.api.codex_models", side_effect=AssertionError("codex should not be fetched")), patch(
        "vibe.api.codex_agents", side_effect=AssertionError("codex should not be fetched")
    ):
        data = asyncio.run(handler._gather_routing_modal_data(context))

    # Listed first, the built-in backend's Agent is still not the fallback while another is listed.
    assert data.current_agent == "id-opencode"
    assert [agent.id for agent in data.agents] == ["id-vibey", "id-opencode", "id-codex"]
    assert _values(data.menus["opencode"].variants) == ["build"]
    assert server.calls == [
        "current_server",
        "agents:/tmp/workspace",
        "models:/tmp/workspace",
        "config:/tmp/workspace",
    ]


def test_slack_agent_switch_back_shows_the_stored_route_not_blanks() -> None:
    """Switching the Slack modal away from the stored Agent and back must not blank what saving would persist."""
    from config.v2_config import SlackConfig
    from modules.im.slack import SlackBot
    from modules.im.slack_modal import parse_routing_modal_selection

    handler, _server = _make_routing_handler()
    routing = RoutingSettings(agent_name="vibey", model="model-b", reasoning_effort="high")
    handler.settings_manager = SimpleNamespace(get_channel_routing=lambda settings_key: routing)
    handler.controller.current = "vibey"
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
    agents = [AgentChoice("id-vibey", "vibey", "vibey"), AgentChoice("id-opencode", "opencode", "opencode")]
    view = bot._build_routing_modal_view(
        channel_id="D0APS47LPU2",
        routing=RoutingModalData(agents=agents, current_agent="id-vibey", current_routing=routing),
        selected_agent="id-opencode",
    )
    view["state"] = {"values": {
        block["block_id"]: {block["element"]["action_id"]: {"selected_option": block["element"]["initial_option"]}}
        for block in view["blocks"] if block["type"] == "input"
    }}
    view["state"]["values"]["agent_block"]["agent_select"] = {"selected_option": {"value": "id-vibey"}}
    selection = parse_routing_modal_selection(
        view=view, action={"action_id": "agent_select", "selected_option": {"value": "id-vibey"}}
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
    assert (shown["route_model:id-vibey"], shown["route_effort:id-vibey"]) == ("model-b", "high")


def test_handle_routing_modal_update_uses_the_visible_current_agent() -> None:
    handler, _server = _make_routing_handler()
    handler.config.claude.enabled = False
    handler.controller.current = "claude"
    update_calls = []

    async def _update_routing_modal(**kwargs):
        update_calls.append(kwargs)

    handler._get_im_client = lambda context: SimpleNamespace(update_routing_modal=_update_routing_modal)
    selection = RoutingModalSelection(selected_agent="id-opencode")

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
    assert update_calls[0]["routing"].current_agent == "id-opencode"
    assert update_calls[0]["selected_agent"] == "id-opencode"


def test_a_routing_save_the_store_refuses_is_reported_not_confirmed(tmp_path, monkeypatch) -> None:
    """IM forms used to confirm a routing change whose save had failed, because the save swallowed errors."""
    from config import paths
    from config.v2_settings import ChannelSettings, SettingsStore
    from modules.settings_manager import SettingsManager

    monkeypatch.setattr(paths, "ensure_data_dirs", lambda: None)
    SettingsStore.reset_instance()
    manager = SettingsManager(settings_file=str(tmp_path / "settings.json"), platform="telegram")
    try:
        manager.store.update_channel("-100123", ChannelSettings(enabled=True), platform="telegram")
        handler, send_message = _make_handler(manager)
        handler.controller._get_settings_key = lambda _context: "-100123"
        # Listed by the form, but not an enabled Agent the store can bind.
        handler.controller.vibe_agent_store.agents.append(
            SimpleNamespace(id="id-gone", name="gone", backend="claude", model=None, enabled=True)
        )

        with patch.object(handler, "_routing_update_needs_new_session_hint", return_value=False):
            asyncio.run(
                handler.handle_routing_update(
                    user_id="42", channel_id="-100123", agent_id="id-gone", platform="telegram"
                )
            )

        text = send_message.await_args.args[1]
        assert text.startswith("❌"), text
        assert "Agent `gone` is unavailable for this route." in text
        assert manager.get_channel_routing("-100123").agent_name is None
        assert manager.store.find_channel("-100123", platform="telegram").routing.agent_name is None
    finally:
        SettingsStore.reset_instance()


def test_scope_needs_new_session_answers_for_the_scope_the_web_saved() -> None:
    """A DM session is anchored on the platform's DM channel; only a flat scope's live session keeps its Agent."""
    rows = {"discord_D-chan": {"agent_name": "opencode", "agent_backend": "opencode"}}
    anchors: list[str] = []

    def find_session_for_anchor(session_key, anchor):
        anchors.append(anchor)
        return rows.get(anchor)

    def handler_for(*, threaded: bool, routing_agent: str):
        manager = SimpleNamespace(
            get_channel_routing=lambda key: RoutingSettings(agent_name=routing_agent),
            get_store=lambda: SimpleNamespace(
                get_user=lambda user_id, platform=None: SimpleNamespace(dm_chat_id="D-chan") if user_id == "U1" else None
            ),
        )
        controller = SimpleNamespace(
            config=SimpleNamespace(platform="discord", language="en"),
            im_client=SimpleNamespace(
                should_use_thread_for_reply=lambda: threaded,
                should_use_thread_for_dm_session=lambda: threaded,
                should_use_message_id_for_channel_session=lambda context=None: threaded,
            ),
            settings_manager=manager,
            sessions=SimpleNamespace(find_session_for_anchor=find_session_for_anchor),
            session_handler=SimpleNamespace(get_base_session_id=lambda context: f"{context.platform}_{context.channel_id}"),
            vibe_agent_store=_AgentStore(),
            _get_settings_key=lambda context: context.user_id or context.channel_id,
            _get_session_key=lambda context: f"{context.platform}::{context.channel_id}",
            _get_lang=lambda: "en",
        )
        return SettingsHandler(controller)

    flat = handler_for(threaded=False, routing_agent="vibey")
    assert flat.scope_needs_new_session(platform="discord", kind="user", native_id="U1") is True
    assert anchors[-1] == "discord_D-chan"
    assert handler_for(threaded=False, routing_agent="opencode").scope_needs_new_session(
        platform="discord", kind="user", native_id="U1"
    ) is False
    assert flat.scope_needs_new_session(platform="discord", kind="channel", native_id="C9") is False
    assert handler_for(threaded=True, routing_agent="vibey").scope_needs_new_session(
        platform="discord", kind="user", native_id="U1"
    ) is False
