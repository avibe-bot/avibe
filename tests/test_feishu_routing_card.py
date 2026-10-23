import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from config.v2_config import LarkConfig
from config.v2_settings import RoutingSettings
from core.modals import BackendRouteMenu, RouteOption, RoutingModalData
from core.auth import AuthResult
from modules.im.base import MessageContext
from modules.im.feishu import FeishuBot


class FeishuRoutingCardTests(unittest.IsolatedAsyncioTestCase):
    def _make_bot(self) -> FeishuBot:
        return FeishuBot(LarkConfig(app_id="app-id", app_secret="app-secret"))

    @staticmethod
    def _find_select(card, field_name):
        form = card["body"]["elements"][0]
        for element in form["elements"]:
            if element.get("tag") == "select_static" and element.get("name") == field_name:
                return element
        raise AssertionError(f"select_static {field_name} not found")

    @staticmethod
    def _claude_routing() -> RoutingModalData:
        efforts = [RouteOption("high", "High"), RouteOption("max", "Max")]
        return RoutingModalData(
            registered_backends=["claude", "codex"],
            current_backend="claude",
            current_routing=RoutingSettings(agent_name="claude", model="claude-sonnet-4-5", claude_agent="helper"),
            menus={
                "claude": BackendRouteMenu(
                    "claude",
                    models=[RouteOption(m, m) for m in ("claude-sonnet-4-5", "claude-sonnet-4-6", "claude-opus-4-6")],
                    efforts={"claude-sonnet-4-5": efforts[:1], "claude-opus-4-6": efforts},
                ),
                "codex": BackendRouteMenu("codex", models=[RouteOption("gpt-5.4", "gpt-5.4")]),
            },
        )

    async def test_select_changes_with_different_options_are_not_deduped(self):
        bot = self._make_bot()
        bot._patch_card_message = AsyncMock()
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True)
        bot._routing_cache["chat:user"] = {"routing": self._claude_routing(), "drafts": {}}

        def _event(model):
            return {
                "operator": {"open_id": "user"},
                "context": {"open_message_id": "om_123", "open_chat_id": "chat"},
                "action": {"tag": "select_static", "name": "route_model:claude", "option": {"value": model}},
            }

        await bot._async_handle_card_action(_event("claude-sonnet-4-6"))
        await bot._async_handle_card_action(_event("claude-opus-4-6"))

        self.assertEqual(bot._patch_card_message.await_count, 2)
        self.assertEqual(bot._routing_cache["chat:user"]["drafts"]["claude"].model, "claude-opus-4-6")
        _, latest_card = bot._patch_card_message.await_args.args
        reasoning_values = [
            option["value"] for option in self._find_select(latest_card, "route_effort:claude")["options"]
        ]
        self.assertIn("max", reasoning_values)

    async def test_routing_submit_keeps_the_drafts_native_agent_when_the_form_omits_it(self):
        bot = self._make_bot()
        bot._on_routing_update = AsyncMock()
        routing = RoutingModalData(
            registered_backends=["codex"],
            current_backend="codex",
            current_routing=RoutingSettings(agent_name="codex", model="gpt-5.1", codex_agent="reviewer"),
        )
        bot._routing_cache["chat:user"] = {"routing": routing, "drafts": {}}
        context = MessageContext(user_id="user", channel_id="chat", platform="lark", platform_specific={"is_dm": False})

        await bot._handle_routing_form_submit(
            context, {"route_model:codex": "gpt-5.4", "route_effort:codex": "high"}, "routing_submit:codex"
        )

        bot._on_routing_update.assert_awaited_once_with(
            user_id="user",
            channel_id="chat",
            backend="codex",
            variant="reviewer",
            model="gpt-5.4",
            reasoning_effort="high",
            is_dm=False,
        )

    async def test_an_options_card_saves_its_own_backend_after_another_was_opened(self):
        bot = self._make_bot()
        bot._send_card_to_channel = AsyncMock()
        bot._on_routing_update = AsyncMock()
        context = MessageContext(user_id="user", channel_id="chat", platform="lark", platform_specific={"is_dm": False})
        await bot.open_routing_modal(trigger_id=context, channel_id="chat", routing=self._claude_routing())
        # The step-1 card stays active, so it can open a second backend's card before the first is submitted.
        await bot._handle_routing_backend_select(context, {"backend": "claude"})
        claude_card = bot._send_card_to_channel.await_args.args[1]
        await bot._handle_routing_backend_select(context, {"backend": "codex"})

        elements = claude_card["body"]["elements"][0]["elements"]
        button = next(element for element in elements if element["tag"] == "button")
        await bot._handle_routing_form_submit(
            context,
            {e["name"]: e["initial_option"] for e in elements if e["tag"] == "select_static"},
            button["name"],
        )

        kwargs = bot._on_routing_update.await_args.kwargs
        self.assertEqual((kwargs["backend"], kwargs["model"]), ("claude", "claude-sonnet-4-5"))

    async def test_settings_submit_binds_optional_fields_by_name(self):
        bot = self._make_bot()
        bot._on_settings_update = AsyncMock()
        context = MessageContext(
            user_id="user",
            channel_id="chat",
            platform="lark",
            platform_specific={"is_dm": False},
        )

        await bot._handle_settings_form_submit(
            context,
            {
                "show_message_types": ["assistant"],
                "require_mention": "false",
                "language": "zh",
            },
        )

        bot._on_settings_update.assert_awaited_once()
        call = bot._on_settings_update.await_args
        self.assertEqual(call.args, ())
        self.assertEqual(
            call.kwargs,
            {
                "user_id": "user",
                "show_message_types": ["assistant"],
                "channel_id": "chat",
                "require_mention": False,
                "language": "zh",
                "is_dm": False,
            },
        )

    async def test_quick_reply_card_action_sets_lark_platform(self):
        bot = self._make_bot()
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True)
        bot.on_callback_query_callback = AsyncMock()

        event = {
            "operator": {"open_id": "user"},
            "context": {"open_message_id": "om_123", "open_chat_id": "chat"},
            "action": {
                "tag": "button",
                "value": {"key": "quick_reply:继续"},
            },
        }

        await bot._async_handle_card_action(event)

        bot.on_callback_query_callback.assert_awaited_once()
        callback_context, callback_data = bot.on_callback_query_callback.await_args.args
        self.assertEqual(callback_data, "quick_reply:继续")
        self.assertEqual(callback_context.platform, "lark")
        self.assertEqual(callback_context.platform_specific["platform"], "lark")

    async def test_remove_inline_keyboard_prefers_cached_text(self):
        bot = self._make_bot()
        bot.edit_message = AsyncMock(return_value=True)
        bot._fetch_message_card_content = AsyncMock(return_value=None)
        bot._message_text_cache["om_123"] = "Original body"

        ok = await bot.remove_inline_keyboard(
            MessageContext(user_id="user", channel_id="chat", platform="lark"),
            "om_123",
        )

        self.assertTrue(ok)
        bot.edit_message.assert_awaited_once()
        _, kwargs = bot.edit_message.await_args
        self.assertEqual(kwargs["text"], "Original body")
        self.assertIsNone(kwargs["keyboard"])
        bot._fetch_message_card_content.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
