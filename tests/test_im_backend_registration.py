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
from modules.im import MessageContext
from modules.im.discord import DiscordBot
from modules.im.feishu import FeishuBot
from modules.im.slack import SlackBot
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
        channel_id="fixture-channel", registered_backends=["codex", "vibey"],
        current_backend="vibey", current_routing=None,
        opencode_agents=[], opencode_models={}, opencode_default_config={},
        claude_agents=[], claude_models=[], codex_agents=[], codex_models=[],
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


def _efforts(*values):
    return [{"value": "__default__", "label": "(Default)"}, *({"value": v, "label": v.title()} for v in values)]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "discord", "telegram", "feishu"])
async def test_vibey_route_offers_its_models_and_efforts_and_saves_the_choice(platform):
    """A channel routed to Vibey can see and keep its model and effort in every IM routing UI.

    The stored model sits past every platform's option cap, and Model Hub no longer offers the stored effort:
    saving unchanged must still keep both.
    """
    long_id = "vendor/" + "x" * 160  # longer than a Slack or Discord option value may be
    routing = dict(
        registered_backends=["vibey", "codex"], current_backend="vibey",
        current_routing=RoutingSettings(agent_name="vibey", model="model-b", reasoning_effort="max"),
        opencode_agents=[], opencode_models={}, opencode_default_config={},
        claude_agents=[], claude_models=[], codex_agents=[], codex_models=[],
        vibey_models=[
            {"value": "model-a", "label": "Model A"},
            *({"value": f"filler-{i}", "label": f"Filler {i}"} for i in range(120)),
            {"value": long_id, "label": "Long"},
            {"value": "model-b", "label": "Model B"},
        ],
        backend_reasoning_options={"vibey": {"model-a": _efforts("low"), "model-b": _efforts("high")}},
    )
    context = MessageContext(user_id="42", channel_id="fixture-channel", message_id="88", platform=platform,
                             platform_specific={"is_dm": False})
    saved = AsyncMock()

    if platform == "slack":
        bot = SlackBot(SlackConfig(bot_token="fixture-token"))
        view = bot._build_routing_modal_view(channel_id=context.channel_id, **routing)
        offered = list(_options(view))
        # Saving unchanged submits what each input shows.
        view["state"] = {"values": {
            block["block_id"]: {block["element"]["action_id"]: {"selected_option": block["element"]["initial_option"]}}
            for block in view["blocks"] if block["type"] == "input"
        }}
        bot._on_routing_update = saved
        await bot._handle_view_submission({"view": view, "user": {"id": context.user_id}})
    elif platform == "discord":
        bot = DiscordBot(DiscordConfig(bot_token="fixture-token"))
        channel = SimpleNamespace(send=AsyncMock())
        bot._fetch_channel = AsyncMock(return_value=channel)
        await bot.open_routing_modal(trigger_id=None, channel_id=context.channel_id, **routing)
        view = channel.send.await_args.kwargs["view"]
        offered = [{"value": o.value, "text": o.label} for child in view.children for o in getattr(child, "options", [])]
        bot.check_authorization = Mock(return_value=SimpleNamespace(allowed=True))
        bot._dismiss_interaction_message = AsyncMock()
        bot._on_routing_update = saved
        await view._on_save(SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()), user=SimpleNamespace(id=42), channel_id=None, guild=None,
        ))
    elif platform == "telegram":
        bot = TelegramBot(TelegramConfig(bot_token="123456:fixture-token"))
        bot.send_message_with_buttons = AsyncMock(return_value="88")
        await bot.open_routing_modal(context, context.channel_id, **routing)
        keyboard = bot.send_message_with_buttons.await_args.args[2]
        fields = {button.callback_data for row in keyboard.buttons for button in row}
        assert {"tg_route:field:vibey_model", "tg_route:field:vibey_reasoning_effort"} <= fields
        state = bot._routing_states[bot._interaction_scope_key(context)]
        offered = []
        for field in ("vibey_model", "vibey_reasoning_effort"):
            state.picker_field = field
            offered += [{"value": value, "text": label} for label, value in bot._routing_picker_options(state)]
        state.picker_field = None
        bot._delete_interaction_message = AsyncMock()
        bot._controller = SimpleNamespace(settings_handler=SimpleNamespace(handle_routing_update=saved))
        await bot._handle_routing_callback(context, "tg_route:save")
    else:
        bot = FeishuBot(LarkConfig(app_id="fixture-app", app_secret="fixture-secret"))
        bot._send_card_to_channel = AsyncMock()
        await bot.open_routing_modal(trigger_id=context, channel_id=context.channel_id, **routing)
        await bot._handle_routing_backend_select(context, {"backend": "vibey"})
        card = bot._send_card_to_channel.await_args.args[1]
        offered = list(_options(card))
        selects = [el for el in card["body"]["elements"][0]["elements"] if el["tag"] == "select_static"]
        bot._on_routing_update = saved
        await bot._handle_routing_form_submit(
            context, {"backend": "vibey", **{el["name"]: el["initial_option"] for el in selects}},
        )

    values = {option["value"] for option in offered}
    assert {"model-a", "model-b", "high", "max"} <= values
    assert "low" not in values  # model-a's efforts are not model-b's
    assert "Model B" in json.dumps(offered)
    if platform in {"slack", "discord"}:
        assert long_id not in values  # one such id must not make the whole view invalid
    saved.assert_awaited_once()
    assert saved.await_args.kwargs["backend"] == "vibey"
    assert (saved.await_args.kwargs["vibey_model"], saved.await_args.kwargs["vibey_reasoning_effort"]) == (
        "model-b", "max",
    )
