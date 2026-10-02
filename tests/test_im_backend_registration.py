"""C-8 IM payload boundary: Agent selection includes Avibe, native resume does not.

The existing per-platform picker tests use only native backends and cannot catch
accidentally sharing a whole-universe list with a native-session selector.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config.v2_config import DiscordConfig, LarkConfig, SlackConfig
from modules.im.discord import DiscordBot
from modules.im.feishu import FeishuBot
from modules.im.slack import SlackBot


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
async def test_agent_picker_includes_avibe_but_native_resume_excludes_it(platform):
    routing = dict(
        channel_id="fixture-channel", registered_backends=["codex", "avibe"],
        current_backend="avibe", current_routing=None,
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
    avibe = next(option for option in options if option["value"] == "avibe")
    assert "Avibe Agent" in json.dumps(avibe)

    bot._controller = SimpleNamespace(agent_service=SimpleNamespace(agents={"codex": object(), "avibe": object()}))
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
    assert "avibe" not in values
