"""C-8 IM payload boundary: Agent selection includes Avibe, native resume does not.

The existing per-platform picker tests use only native backends and cannot catch
accidentally sharing a whole-universe list with a native-session selector.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config.v2_config import DiscordConfig, LarkConfig, SlackConfig, TelegramConfig
from config.v2_settings import RoutingSettings
from core.modals import BackendRouteMenu, RouteOption, RoutingModalData
from modules.agents.catalog import display_name_for_backend
from modules.im import MessageContext
from modules.im.discord import DiscordBot
from modules.im.feishu import FeishuBot
from modules.im.slack import SlackBot
from modules.im.slack_modal import parse_routing_modal_selection
from modules.im.telegram import TelegramBot


def _options(payload):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "options":
                yield from value
            else:
                yield from _options(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _options(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "discord", "feishu"])
async def test_agent_picker_includes_vibey_but_native_resume_excludes_it(platform):
    routing = dict(
        channel_id="fixture-channel",
        routing=RoutingModalData(registered_backends=["codex", "vibey"], current_backend="vibey", current_routing=None),
    )
    if platform == "slack":
        bot = SlackBot(SlackConfig(bot_token="fixture-token"))
        bot._ensure_clients = Mock()
        bot.web_client = SimpleNamespace(views_open=AsyncMock())
        options = list(_options(bot._build_routing_modal_view(**routing)))
    elif platform == "discord":
        bot = DiscordBot(DiscordConfig(bot_token="fixture-token"))
        channel = SimpleNamespace(send=AsyncMock())
        bot._fetch_channel = AsyncMock(return_value=channel)
        await bot.open_routing_modal(trigger_id=None, **routing)
        view = channel.send.await_args.kwargs["view"]
        options = [
            {"value": option.value, "text": option.label}
            for child in view.children for option in getattr(child, "options", [])
        ]
    else:
        bot = FeishuBot(LarkConfig(app_id="fixture-app", app_secret="fixture-secret"))
        bot._send_card_to_channel = AsyncMock()
        await bot.open_routing_modal(trigger_id=None, **routing)
        options = list(_options(bot._send_card_to_channel.await_args.args[1]))
    vibey = next(option for option in options if option["value"] == "vibey")
    assert "Vibey" in json.dumps(vibey)

    bot._controller = SimpleNamespace(agent_service=SimpleNamespace(agents={"codex": object(), "vibey": object()}))
    await bot.open_resume_session_modal(
        trigger_id=None, sessions=[], channel_id="fixture-channel", thread_id=None, host_message_ts=None,
    )
    if platform == "slack":
        metadata = json.loads(bot.web_client.views_open.await_args.kwargs["view"]["private_metadata"])
        values = [option["value"] for option in metadata["agent_options"]]
    elif platform == "discord":
        view = channel.send.await_args.kwargs["view"]
        values = [option.value for child in view.children for option in getattr(child, "options", [])]
    else:
        values = [option["value"] for option in _options(bot._send_card_to_channel.await_args.args[1])]
    assert "codex" in values
    assert "vibey" not in values


PLATFORMS = ["slack", "discord", "telegram", "feishu"]


def _efforts(*values):
    return [RouteOption(value, value.title()) for value in values]


def _route(bot, backend):
    """A channel routed to ``backend``: its stored model sits past every platform's option cap, and its stored
    effort is one the menu no longer offers. Saving unchanged must keep both, on every platform."""
    long_id = "vendor/" + "x" * 160  # longer than a Slack or Discord option value may be
    menu = BackendRouteMenu(
        backend,
        variants=_efforts("builder", "reviewer") if backend == "codex" else [],
        models=[
            RouteOption("model-a", "Model A"),
            *(RouteOption(f"filler-{i}", f"Filler {i}") for i in range(120)),
            RouteOption(long_id, "Long"),
            RouteOption("model-b", "Model B"),
        ],
        efforts={"model-a": _efforts("low"), "model-b": _efforts("high")},
        # What the platform declares, which the settings handler reads into every menu it builds.
        option_limit=getattr(bot, "route_option_limit", None),
        value_limit=getattr(bot, "route_option_value_limit", None),
    )
    stored = RoutingSettings(
        agent_name=backend, model="model-b", reasoning_effort="max",
        codex_agent="reviewer" if backend == "codex" else None,
    )
    data = RoutingModalData(
        registered_backends=[backend, "opencode"], current_backend=backend, current_routing=stored,
        menus={backend: menu},
    )
    return data, long_id


class _Form:
    """Drive one platform's routing form through its own entry points: open, change the model, save."""

    def __init__(self, platform, backend):
        self.platform = platform
        self.backend = backend
        self.context = MessageContext(user_id="42", channel_id="fixture-channel", message_id="88",
                                      platform=platform, platform_specific={"is_dm": False})
        self.saved = AsyncMock()
        if platform == "slack":
            self.bot = SlackBot(SlackConfig(bot_token="fixture-token"))
        elif platform == "discord":
            self.bot = DiscordBot(DiscordConfig(bot_token="fixture-token"))
        elif platform == "telegram":
            self.bot = TelegramBot(TelegramConfig(bot_token="123456:fixture-token"))
        else:
            self.bot = FeishuBot(LarkConfig(app_id="fixture-app", app_secret="fixture-secret"))
        self.data, self.long_id = _route(self.bot, backend)

    async def open(self):
        bot, context = self.bot, self.context
        if self.platform == "slack":
            self.view = bot._build_routing_modal_view(channel_id=context.channel_id, routing=self.data)
        elif self.platform == "discord":
            channel = SimpleNamespace(send=AsyncMock())
            bot._fetch_channel = AsyncMock(return_value=channel)
            await bot.open_routing_modal(trigger_id=None, channel_id=context.channel_id, routing=self.data)
            self.view = channel.send.await_args.kwargs["view"]
        elif self.platform == "telegram":
            bot.send_message_with_buttons = AsyncMock(return_value="88")
            bot.edit_message = AsyncMock(return_value=True)
            await bot.open_routing_modal(context, context.channel_id, routing=self.data)
            keyboard = bot.send_message_with_buttons.await_args.args[2]
            fields = {button.callback_data for row in keyboard.buttons for button in row}
            assert {"tg_route:field:model", "tg_route:field:reasoning_effort"} <= fields
        else:
            bot._send_card_to_channel = AsyncMock()
            await bot.open_routing_modal(trigger_id=context, channel_id=context.channel_id, routing=self.data)
            await bot._handle_routing_backend_select(context, {"backend": self.backend})
            self.card = bot._send_card_to_channel.await_args.args[1]

    def offered(self, field):
        """The option values the form offers for ``field`` (variant, model or effort) right now."""
        if self.platform == "slack":
            block = next(b for b in self.view["blocks"] if str(b.get("block_id", "")).startswith(f"route_{field}:"))
            return [option["value"] for option in block["element"]["options"]]
        if self.platform == "discord":
            key = {"variant": "selectBackendAgent", "model": "selectModel", "effort": "selectReasoningEffort"}[field]
            placeholder = self.bot._t(f"modal.routing.{key}", backend=display_name_for_backend(self.backend))
            select = next(c for c in self.view.children if getattr(c, "placeholder", None) == placeholder)
            return [option.value for option in select.options]
        if self.platform == "telegram":
            state = self.bot._routing_states[self.bot._interaction_scope_key(self.context)]
            name = {"effort": "reasoning_effort"}.get(field, field)
            return [value or "__default__" for _, value in self.bot._routing_picker_options(state, name)]
        name = f"route_{field}"
        select = next(e for e in self.card["body"]["elements"][0]["elements"] if e.get("name") == name)
        return [option["value"] for option in select["options"]]

    async def change_model(self, model):
        bot, context = self.bot, self.context
        if self.platform == "slack":
            self.view["state"] = _shown_state(self.view)
            selection = parse_routing_modal_selection(view=self.view, action={
                "action_id": "route_model", "block_id": f"route_model:{self.backend}",
                "selected_option": {"value": model},
            })
            self.view = bot._build_routing_modal_view(
                channel_id=context.channel_id, routing=self.data,
                selected_backend=selection.selected_backend, draft=selection.draft,
            )
        elif self.platform == "discord":
            prefix = bot._t("modal.routing.selectModel")
            select = next(c for c in self.view.children if getattr(c, "placeholder", None) == prefix)
            select._values = [model]
            await select.callback(SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock())))
        elif self.platform == "telegram":
            await bot._handle_routing_callback(context, "tg_route:field:model")
            state = bot._routing_states[bot._interaction_scope_key(context)]
            index = [value for _, value in bot._routing_picker_options(state)].index(model)
            await bot._handle_routing_callback(context, f"tg_route:option:{index}")
        else:
            bot._patch_card_message = AsyncMock()
            assert await bot._handle_routing_select_change(context, {"name": "route_model", "option": model})
            self.card = bot._patch_card_message.await_args.args[1]

    async def save(self):
        bot, context = self.bot, self.context
        if self.platform == "slack":
            self.view["state"] = _shown_state(self.view)
            bot._on_routing_update = self.saved
            await bot._handle_view_submission({"view": self.view, "user": {"id": context.user_id}})
        elif self.platform == "discord":
            bot.check_authorization = Mock(return_value=SimpleNamespace(allowed=True))
            bot._dismiss_interaction_message = AsyncMock()
            bot._on_routing_update = self.saved
            await self.view._on_save(SimpleNamespace(
                response=SimpleNamespace(defer=AsyncMock()), user=SimpleNamespace(id=42), channel_id=None, guild=None,
            ))
        elif self.platform == "telegram":
            bot._delete_interaction_message = AsyncMock()
            bot._controller = SimpleNamespace(settings_handler=SimpleNamespace(handle_routing_update=self.saved))
            await bot._handle_routing_callback(context, "tg_route:save")
        else:
            # A Feishu form submits what each of its selects shows.
            selects = [e for e in self.card["body"]["elements"][0]["elements"] if e["tag"] == "select_static"]
            bot._on_routing_update = self.saved
            await bot._handle_routing_form_submit(
                context, {"backend": self.backend, **{e["name"]: e["initial_option"] for e in selects}},
            )
        self.saved.assert_awaited_once()
        kwargs = self.saved.await_args.kwargs
        return kwargs["backend"], kwargs["variant"], kwargs["model"], kwargs["reasoning_effort"]


def _shown_state(view):
    """A Slack view's state as submitted unchanged: each input holds what it shows."""
    return {"values": {
        block["block_id"]: {block["element"]["action_id"]: {"selected_option": block["element"]["initial_option"]}}
        for block in view["blocks"] if block["type"] == "input"
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("backend", ["vibey", "codex"])
async def test_a_route_offers_its_backends_menu_and_saving_unchanged_keeps_it(platform, backend):
    form = _Form(platform, backend)
    await form.open()

    models, efforts = form.offered("model"), form.offered("effort")
    assert {"model-a", "model-b"} <= set(models)
    if platform == "telegram":
        assert {f"filler-{i}" for i in range(120)} <= set(models)  # its picker pages, so nothing is cut
    assert efforts == ["__default__", "high", "max"]  # model-b's, plus the stored effort the menu omits
    if platform in {"slack", "discord"}:
        assert form.long_id not in models  # one such id must not make the whole form invalid
    if backend == "codex":
        assert form.offered("variant") == ["__default__", "builder", "reviewer"]
    stored_variant = "reviewer" if backend == "codex" else None
    assert await form.save() == (backend, stored_variant, "model-b", "max")


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", PLATFORMS)
async def test_a_model_change_offers_that_models_efforts_and_clears_the_effort(platform):
    form = _Form(platform, "vibey")
    await form.open()

    await form.change_model("model-a")

    assert form.offered("effort") == ["__default__", "low"]
    assert await form.save() == ("vibey", None, "model-a", None)
