import importlib.util
import asyncio
import unittest
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from config.v2_config import SlackConfig
from modules.im.base import InlineButton, InlineKeyboard, MessageContext


def _install_slack_stubs() -> None:
    if "aiohttp" not in sys.modules:
        try:
            __import__("aiohttp")
        except ImportError:
            aiohttp_mod = types.ModuleType("aiohttp")

            class _ClientWebSocketResponse:
                closed = False

            class _ClientSession:
                async def close(self):
                    return None

            class _ClientTimeout:
                def __init__(self, *args, **kwargs):
                    pass

            aiohttp_mod.ClientWebSocketResponse = _ClientWebSocketResponse
            aiohttp_mod.ClientSession = _ClientSession
            aiohttp_mod.ClientTimeout = _ClientTimeout
            sys.modules["aiohttp"] = aiohttp_mod

    if "markdown_to_mrkdwn" not in sys.modules:
        markdown_mod = types.ModuleType("markdown_to_mrkdwn")

        class _SlackMarkdownConverter:
            def convert(self, text):
                return text

        markdown_mod.SlackMarkdownConverter = _SlackMarkdownConverter
        sys.modules["markdown_to_mrkdwn"] = markdown_mod

    if "slack_sdk" in sys.modules:
        pass

    slack_sdk = types.ModuleType("slack_sdk")
    web_mod = types.ModuleType("slack_sdk.web")
    web_async_mod = types.ModuleType("slack_sdk.web.async_client")
    socket_mode_mod = types.ModuleType("slack_sdk.socket_mode")
    socket_mode_aiohttp_mod = types.ModuleType("slack_sdk.socket_mode.aiohttp")
    socket_mode_request_mod = types.ModuleType("slack_sdk.socket_mode.request")
    socket_mode_response_mod = types.ModuleType("slack_sdk.socket_mode.response")
    errors_mod = types.ModuleType("slack_sdk.errors")

    class _AsyncWebClient:
        def __init__(self, *args, **kwargs):
            pass

        async def auth_test(self):
            return {"user_id": "U_BOT"}

    class _SocketModeClient:
        def __init__(self, *args, **kwargs):
            pass

    class _SocketModeRequest:
        pass

    class _SocketModeResponse:
        def __init__(self, *args, **kwargs):
            pass

    class _SlackApiError(Exception):
        def __init__(self, message="", response=None):
            super().__init__(message)
            self.response = response

    web_async_mod.AsyncWebClient = _AsyncWebClient
    socket_mode_aiohttp_mod.SocketModeClient = _SocketModeClient
    socket_mode_request_mod.SocketModeRequest = _SocketModeRequest
    socket_mode_response_mod.SocketModeResponse = _SocketModeResponse
    errors_mod.SlackApiError = _SlackApiError

    if "slack_sdk" not in sys.modules:
        sys.modules["slack_sdk"] = slack_sdk
        sys.modules["slack_sdk.web"] = web_mod
        sys.modules["slack_sdk.web.async_client"] = web_async_mod
        sys.modules["slack_sdk.socket_mode"] = socket_mode_mod
        sys.modules["slack_sdk.socket_mode.aiohttp"] = socket_mode_aiohttp_mod
        sys.modules["slack_sdk.socket_mode.request"] = socket_mode_request_mod
        sys.modules["slack_sdk.socket_mode.response"] = socket_mode_response_mod
        sys.modules["slack_sdk.errors"] = errors_mod

    if "modules.agents.opencode.utils" not in sys.modules:
        repo_root = Path(__file__).resolve().parents[1]
        agents_mod = types.ModuleType("modules.agents")
        agents_mod.__path__ = [str(repo_root / "modules" / "agents")]
        opencode_mod = types.ModuleType("modules.agents.opencode")
        opencode_mod.__path__ = [str(repo_root / "modules" / "agents" / "opencode")]

        sys.modules["modules.agents"] = agents_mod
        sys.modules["modules.agents.opencode"] = opencode_mod

        spec = importlib.util.spec_from_file_location(
            "modules.agents.opencode.utils",
            repo_root / "modules" / "agents" / "opencode" / "utils.py",
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["modules.agents.opencode.utils"] = module
        spec.loader.exec_module(module)


_install_slack_stubs()

from modules.im.slack import SlackApiError, SlackBot


class _ResponseLike:
    def __init__(self, data):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


class SlackDmMentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_reaction_maps_unicode_robot_to_slack_name(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        calls = []

        class _WebClient:
            async def reactions_add(self, **kwargs):
                calls.append(kwargs)

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")

        ok = await slack.add_reaction(context, "1710000000.000010", "🤖")

        self.assertTrue(ok)
        self.assertEqual(calls[0]["name"], "robot_face")

    async def test_send_message_does_not_set_unfurl_params_by_default(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                return {"ts": "1710000000.000010"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")

        message_ts = await slack.send_message(context, "https://example.com", parse_mode="markdown")

        self.assertEqual(message_ts, "1710000000.000010")
        self.assertNotIn("unfurl_links", sent_payloads[0])
        self.assertNotIn("unfurl_media", sent_payloads[0])

    async def test_send_markdown_message_uses_native_markdown_block_with_buttons(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                return {"ts": "1710000000.000020"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="Continue", callback_data="quick_reply:Continue")]])
        text = "| Col 1 | Col 2 |\n| ----- | ----- |\n| A | B |"

        message_ts = await slack.send_markdown_message(context, text, keyboard=keyboard)

        self.assertEqual(message_ts, "1710000000.000020")
        self.assertEqual(sent_payloads[0]["text"], text)
        self.assertEqual(sent_payloads[0]["blocks"][0], {"type": "markdown", "text": text})
        self.assertEqual(sent_payloads[0]["blocks"][1]["type"], "actions")
        self.assertEqual(sent_payloads[0]["blocks"][1]["elements"][0]["value"], "quick_reply:Continue")

    async def test_send_markdown_message_falls_back_when_native_block_is_rejected(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                if len(sent_payloads) == 1:
                    raise SlackApiError("invalid blocks", response=_ResponseLike({"error": "invalid_blocks"}))
                return {"ts": "1710000000.000021"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")
        text = "| Col 1 | Col 2 |\n| ----- | ----- |\n| A | B |"

        message_ts = await slack.send_markdown_message(context, text)

        self.assertEqual(message_ts, "1710000000.000021")
        self.assertEqual(sent_payloads[0]["blocks"][0], {"type": "markdown", "text": text})
        self.assertEqual(sent_payloads[1]["blocks"][0]["type"], "section")
        self.assertEqual(sent_payloads[1]["blocks"][0]["text"]["type"], "mrkdwn")

    async def test_send_markdown_message_long_button_fallback_preserves_reply_to(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                return {"ts": "1710000000.000022"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="Continue", callback_data="quick_reply:Continue")]])
        text = "x" * 12001

        message_ts = await slack.send_markdown_message(
            context,
            text,
            keyboard=keyboard,
            reply_to="1710000000.000001",
        )

        self.assertEqual(message_ts, "1710000000.000022")
        self.assertGreater(len(sent_payloads), 1)
        self.assertTrue(all(payload["thread_ts"] == "1710000000.000001" for payload in sent_payloads))
        self.assertEqual(sent_payloads[-1]["blocks"][-1]["type"], "actions")

    async def test_send_markdown_message_button_rejection_fallback_preserves_reply_to(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                if len(sent_payloads) == 1:
                    raise SlackApiError("invalid blocks", response=_ResponseLike({"error": "invalid_blocks"}))
                return {"ts": "1710000000.000023"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="Continue", callback_data="quick_reply:Continue")]])

        message_ts = await slack.send_markdown_message(
            context,
            "markdown table",
            keyboard=keyboard,
            reply_to="1710000000.000001",
        )

        self.assertEqual(message_ts, "1710000000.000023")
        self.assertEqual(sent_payloads[0]["thread_ts"], "1710000000.000001")
        self.assertEqual(sent_payloads[1]["thread_ts"], "1710000000.000001")
        self.assertEqual(sent_payloads[1]["blocks"][-1]["type"], "actions")

    async def test_send_message_disables_link_unfurl_when_configured(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", disable_link_unfurl=True))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                return {"ts": "1710000000.000011"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")

        message_ts = await slack.send_message(context, "https://example.com", parse_mode="markdown")

        self.assertEqual(message_ts, "1710000000.000011")
        self.assertIs(sent_payloads[0]["unfurl_links"], False)
        self.assertIs(sent_payloads[0]["unfurl_media"], False)

    async def test_send_dm_disables_link_unfurl_when_configured(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", disable_link_unfurl=True))
        sent_payloads = []

        class _WebClient:
            async def conversations_open(self, users):
                return {"ok": True, "channel": {"id": "D123"}}

            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                return {"ts": "1710000000.000012"}

        slack.web_client = _WebClient()

        message_ts = await slack.send_dm("U123", "https://example.com")

        self.assertEqual(message_ts, "1710000000.000012")
        self.assertEqual(sent_payloads[0]["channel"], "D123")
        self.assertIs(sent_payloads[0]["unfurl_links"], False)
        self.assertIs(sent_payloads[0]["unfurl_media"], False)

    async def test_send_message_with_buttons_disables_link_unfurl_when_configured(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", disable_link_unfurl=True))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                return {"ts": "1710000000.000013"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="Open", callback_data="open")]])

        message_ts = await slack.send_message_with_buttons(
            context,
            "https://example.com",
            keyboard,
            parse_mode="markdown",
        )

        self.assertEqual(message_ts, "1710000000.000013")
        self.assertIs(sent_payloads[0]["unfurl_links"], False)
        self.assertIs(sent_payloads[0]["unfurl_media"], False)
        self.assertEqual(
            sent_payloads[0]["blocks"][0]["text"]["text"],
            "<https://example.com>",
        )
        self.assertIs(sent_payloads[0]["blocks"][0]["text"]["verbatim"], True)

    async def test_send_message_with_buttons_linkifies_only_bare_urls_for_verbatim_blocks(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", disable_link_unfurl=True))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                return {"ts": "1710000000.000014"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="Open", callback_data="open")]])
        text = (
            "Open https://example.com/path?a=1&b=2.\n"
            "Existing <https://example.org|Example>.\n"
            "Mail <mailto:help@example.com|Visit https://status.example.com now>.\n"
            "Date <!date^1392734382^{date_short}^https://date.example/|Feb 18>.\n"
            "Comparison 1 < 2 https://compare.example.\n"
            "Inline `https://code.example`.\n"
            "```\nhttps://block.example\n```"
        )

        await slack.send_message_with_buttons(context, text, keyboard, parse_mode="markdown")

        block_text = sent_payloads[0]["blocks"][0]["text"]["text"]
        self.assertIn("<https://example.com/path?a=1&b=2>.", block_text)
        self.assertIn("<https://example.org|Example>", block_text)
        self.assertIn("<mailto:help@example.com|Visit https://status.example.com now>", block_text)
        self.assertIn("<!date^1392734382^{date_short}^https://date.example/|Feb 18>", block_text)
        self.assertIn("Comparison 1 < 2 <https://compare.example>.", block_text)
        self.assertIn("`https://code.example`", block_text)
        self.assertIn("```\nhttps://block.example\n```", block_text)
        self.assertNotIn("<<https://example.org", block_text)

    async def test_send_message_with_buttons_keeps_default_preview_behavior(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                return {"ts": "1710000000.000015"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="Open", callback_data="open")]])

        await slack.send_message_with_buttons(
            context,
            "https://example.com",
            keyboard,
            parse_mode="markdown",
        )

        self.assertEqual(sent_payloads[0]["blocks"][0]["text"]["text"], "https://example.com")
        self.assertNotIn("unfurl_links", sent_payloads[0])
        self.assertNotIn("unfurl_media", sent_payloads[0])

    async def test_send_message_with_buttons_splits_when_linkified_text_exceeds_section_limit(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", disable_link_unfurl=True))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                return {"ts": f"1710000000.00001{len(sent_payloads)}"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="Open", callback_data="open")]])
        text = " ".join(["https://example.com/" + ("x" * 60) for _ in range(45)])

        await slack.send_message_with_buttons(context, text, keyboard, parse_mode="markdown")

        self.assertGreater(len(sent_payloads), 1)
        final_payload = sent_payloads[-1]
        self.assertLessEqual(len(final_payload["blocks"][0]["text"]["text"]), 3000)
        self.assertEqual(final_payload["blocks"][1]["type"], "actions")

    async def test_send_message_with_buttons_keeps_linkified_long_url_under_section_limit(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", disable_link_unfurl=True))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                return {"ts": f"1710000000.00002{len(sent_payloads)}"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="Open", callback_data="open")]])
        long_url = "https://example.com/" + ("x" * 1500)

        await slack.send_message_with_buttons(
            context,
            f"{long_url} tail",
            keyboard,
            parse_mode="markdown",
        )

        block_text = sent_payloads[-1]["blocks"][0]["text"]["text"]
        self.assertIn(f"<{long_url}>", block_text)
        self.assertLessEqual(len(block_text), 3000)
        self.assertEqual(sent_payloads[-1]["blocks"][1]["type"], "actions")

    async def test_linkify_skips_url_too_long_for_explicit_slack_link(self):
        long_url = "https://example.com/" + ("x" * 2980)

        text = SlackBot._linkify_bare_urls_for_verbatim_mrkdwn(long_url)

        self.assertEqual(text, long_url)
        self.assertLessEqual(len(text), 3000)

    async def test_send_message_recovers_dm_channel_after_channel_not_found(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        sent_channels = []
        sent_thread_ts = []

        class _WebClient:
            def __init__(self):
                self.fail_once = True

            async def chat_postMessage(self, **kwargs):
                sent_channels.append(kwargs["channel"])
                sent_thread_ts.append(kwargs.get("thread_ts"))
                if self.fail_once:
                    self.fail_once = False
                    raise sys.modules["slack_sdk.errors"].SlackApiError(
                        "channel missing",
                        response={"error": "channel_not_found"},
                    )
                return {"ts": "1710000000.000001"}

            async def conversations_open(self, users):
                return {"ok": True, "channel": {"id": "D999"}}

        slack.web_client = _WebClient()
        context = MessageContext(
            user_id="U123",
            channel_id="D123",
            thread_id="1710000000.000100",
            platform_specific={"is_dm": True},
        )

        message_ts = await slack.send_message(context, "hello", parse_mode="markdown")

        self.assertEqual(message_ts, "1710000000.000001")
        self.assertEqual(sent_channels, ["D123", "D999"])
        self.assertEqual(sent_thread_ts, ["1710000000.000100", None])
        self.assertEqual(context.channel_id, "D999")
        self.assertIsNone(context.thread_id)

    async def test_send_message_with_buttons_recovers_dm_channel_after_channel_not_found(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        sent_channels = []
        sent_thread_ts = []

        class _WebClient:
            def __init__(self):
                self.fail_once = True

            async def chat_postMessage(self, **kwargs):
                sent_channels.append(kwargs["channel"])
                sent_thread_ts.append(kwargs.get("thread_ts"))
                if self.fail_once:
                    self.fail_once = False
                    raise sys.modules["slack_sdk.errors"].SlackApiError(
                        "channel missing",
                        response={"error": "channel_not_found"},
                    )
                return {"ts": "1710000000.000002"}

            async def conversations_open(self, users):
                return {"ok": True, "channel": {"id": "D999"}}

        slack.web_client = _WebClient()
        context = MessageContext(
            user_id="U123",
            channel_id="D123",
            thread_id="1710000000.000100",
            platform_specific={"is_dm": True},
        )
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="One", callback_data="choose:1")]])

        message_ts = await slack.send_message_with_buttons(context, "hello", keyboard, parse_mode="markdown")

        self.assertEqual(message_ts, "1710000000.000002")
        self.assertEqual(sent_channels, ["D123", "D999"])
        self.assertEqual(sent_thread_ts, ["1710000000.000100", None])
        self.assertEqual(context.channel_id, "D999")
        self.assertIsNone(context.thread_id)

    async def test_send_message_recovers_stale_dm_context_even_when_bound_channel_changed(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        sent_channels = []

        class _WebClient:
            def __init__(self):
                self.fail_once = True

            async def chat_postMessage(self, **kwargs):
                sent_channels.append(kwargs["channel"])
                if self.fail_once:
                    self.fail_once = False
                    raise sys.modules["slack_sdk.errors"].SlackApiError(
                        "channel missing",
                        response={"error": "channel_not_found"},
                    )
                return {"ts": "1710000000.000003"}

            async def conversations_open(self, users):
                assert users == ["U123"]
                return {"ok": True, "channel": {"id": "D999"}}

        class _Store:
            def maybe_reload(self):
                return None

            def get_user(self, user_id, platform=None):
                if user_id == "U123":
                    return SimpleNamespace(dm_chat_id="D999")
                return None

        class _SettingsManager:
            def get_store(self):
                return _Store()

        slack.web_client = _WebClient()
        slack.set_settings_manager(_SettingsManager())
        context = MessageContext(
            user_id="U123",
            channel_id="D_OLD",
            thread_id="1710000000.000100",
            platform_specific={"is_dm": True},
        )

        message_ts = await slack.send_message(context, "hello", parse_mode="markdown")

        self.assertEqual(message_ts, "1710000000.000003")
        self.assertEqual(sent_channels, ["D_OLD", "D999"])
        self.assertEqual(context.channel_id, "D999")
        self.assertIsNone(context.thread_id)

    async def test_send_message_with_buttons_splits_long_text_before_button_block(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        sent_payloads = []

        class _WebClient:
            async def chat_postMessage(self, **kwargs):
                sent_payloads.append(kwargs)
                return {"ts": f"1710000000.00000{len(sent_payloads)}"}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="One", callback_data="choose:1")]])
        text = "\n\n".join([f"Paragraph {index} {'x' * 120}" for index in range(30)])

        message_ts = await slack.send_message_with_buttons(context, text, keyboard, parse_mode="markdown")

        self.assertEqual(message_ts, "1710000000.000002")
        self.assertEqual(len(sent_payloads), 2)
        self.assertEqual(sent_payloads[0]["text"] + sent_payloads[1]["text"], text)
        self.assertTrue(all(len(payload["text"]) <= 3000 for payload in sent_payloads))
        self.assertFalse(any(block["type"] == "actions" for block in sent_payloads[0].get("blocks", [])))
        self.assertEqual(sent_payloads[1]["blocks"][0]["type"], "section")
        self.assertEqual(sent_payloads[1]["blocks"][1]["type"], "actions")

    async def test_remove_inline_keyboard_uses_visible_chunk_for_long_text(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        updates = []

        class _WebClient:
            async def chat_update(self, **kwargs):
                updates.append(kwargs)
                return {"ok": True}

        slack.web_client = _WebClient()
        context = MessageContext(user_id="U123", channel_id="C123")
        text = "\n\n".join([f"Paragraph {index} {'x' * 120}" for index in range(30)])

        ok = await slack.remove_inline_keyboard(context, "1710000000.000002", text=text, parse_mode="markdown")

        self.assertTrue(ok)
        self.assertEqual(len(updates), 1)
        self.assertLessEqual(len(updates[0]["text"]), 3000)
        self.assertEqual(updates[0]["text"], slack._get_visible_text(text))
        self.assertEqual(updates[0]["blocks"][0]["type"], "section")

    def test_split_text_keeps_boundary_chunk_within_limit(self):
        chunks = SlackBot._split_text("a" * 3000 + " " + "b", 3000)

        self.assertEqual(chunks, ["a" * 3000, " b"])
        self.assertTrue(all(len(chunk) <= 3000 for chunk in chunks))

    async def test_get_user_info_prefers_normalized_profile_names(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))

        class _WebClient:
            async def users_info(self, user):
                return {
                    "user": {
                        "id": user,
                        "name": "cyh",
                        "real_name": "Alex Raw",
                        "profile": {
                            "display_name": "Alex Raw Display",
                            "display_name_normalized": "Alex",
                            "real_name_normalized": "Alex",
                            "email": "alex@example.com",
                        },
                    }
                }

        slack.web_client = _WebClient()

        user_info = await slack.get_user_info("U0E0FM3QT")

        self.assertEqual(user_info["display_name"], "Alex")
        self.assertEqual(user_info["real_name"], "Alex")
        self.assertEqual(user_info["name"], "cyh")

    async def test_dm_mention_only_falls_through_as_empty_message(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}

        async def _on_message(context, text):
            received["channel_id"] = context.channel_id
            received["thread_id"] = context.thread_id
            received["text"] = text

        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-dm-mention-only",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "D123",
                "user": "U123",
                "text": "<@U_BOT>",
                "ts": "1710000000.000100",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(
            received,
            {
                "channel_id": "D123",
                "thread_id": "1710000000.000100",
                "text": "",
            },
        )

    async def test_dm_mention_with_text_preserves_raw_text_for_agent(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}

        async def _on_message(context, text):
            received["text"] = text

        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-dm-mention-text",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "D123",
                "user": "U123",
                "text": "<@U_BOT> hello",
                "ts": "1710000000.000200",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, {"text": "<@U_BOT> hello"})

    async def test_bound_user_message_from_mismatched_dm_channel_is_ignored(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {"called": False}

        class _Store:
            def maybe_reload(self):
                return None

            def get_user(self, user_id, platform=None):
                if user_id == "U123":
                    return SimpleNamespace(dm_chat_id="D_REAL")
                return None

            def find_channel(self, channel_id, platform=None):
                if channel_id == "C123":
                    return SimpleNamespace(enabled=True)
                return None

            def is_bound_user(self, user_id, platform=None):
                return user_id == "U123"

        class _SettingsManager:
            def get_store(self):
                return _Store()

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        class _WebClient:
            async def conversations_open(self, users):
                assert users == ["U123"]
                return {"ok": True, "channel": {"id": "D_REAL"}}

        async def _on_message(_context, _text):
            received["called"] = True

        slack.web_client = _WebClient()
        slack.set_settings_manager(_SettingsManager())
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-dm-mismatch",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "D_OTHER",
                "user": "U123",
                "text": "hello",
                "ts": "1710000000.000250",
            },
        }

        await slack._handle_event(payload)

        self.assertFalse(received["called"])

    async def test_bound_user_message_repairs_missing_dm_channel_binding(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}
        updates = []

        class _Store:
            def maybe_reload(self):
                return None

            def get_user(self, user_id, platform=None):
                if user_id == "U123":
                    return SimpleNamespace(dm_chat_id="")
                return None

            def update_user(self, user_id, settings, platform=None):
                updates.append((user_id, getattr(settings, "dm_chat_id", ""), platform))

            def is_bound_user(self, user_id, platform=None):
                return user_id == "U123"

        class _SettingsManager:
            def get_store(self):
                return _Store()

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        class _WebClient:
            async def conversations_open(self, users):
                assert users == ["U123"]
                return {"ok": True, "channel": {"id": "D_REAL"}}

        async def _on_message(_context, text):
            received["text"] = text

        slack.web_client = _WebClient()
        slack.set_settings_manager(_SettingsManager())
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-dm-repair-missing-binding",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "D_REAL",
                "user": "U123",
                "text": "hello",
                "ts": "1710000000.000255",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, {"text": "hello"})
        self.assertEqual(updates, [("U123", "D_REAL", "slack")])

    async def test_bound_user_missing_dm_channel_still_ignores_wrong_dm(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {"called": False}

        class _Store:
            def maybe_reload(self):
                return None

            def get_user(self, user_id, platform=None):
                if user_id == "U123":
                    return SimpleNamespace(dm_chat_id="")
                return None

            def update_user(self, user_id, settings, platform=None):
                return None

            def is_bound_user(self, user_id, platform=None):
                return user_id == "U123"

        class _SettingsManager:
            def get_store(self):
                return _Store()

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        class _WebClient:
            async def conversations_open(self, users):
                assert users == ["U123"]
                return {"ok": True, "channel": {"id": "D_REAL"}}

        async def _on_message(_context, _text):
            received["called"] = True

        slack.web_client = _WebClient()
        slack.set_settings_manager(_SettingsManager())
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-dm-missing-binding-wrong-channel",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "D_OTHER",
                "user": "U123",
                "text": "hello",
                "ts": "1710000000.000257",
            },
        }

        await slack._handle_event(payload)

        self.assertFalse(received["called"])

    async def test_bound_user_mismatched_dm_channel_lookup_error_falls_back_to_processing(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}

        class _Store:
            def maybe_reload(self):
                return None

            def get_user(self, user_id, platform=None):
                if user_id == "U123":
                    return SimpleNamespace(dm_chat_id="D_STALE")
                return None

            def is_bound_user(self, user_id, platform=None):
                return user_id == "U123"

        class _SettingsManager:
            def get_store(self):
                return _Store()

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        class _WebClient:
            async def conversations_open(self, users):
                raise sys.modules["slack_sdk.errors"].SlackApiError(
                    "rate limited",
                    response={"error": "ratelimited"},
                )

        async def _on_message(_context, text):
            received["text"] = text

        slack.web_client = _WebClient()
        slack.set_settings_manager(_SettingsManager())
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-dm-lookup-error",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "D_REAL",
                "user": "U123",
                "text": "hello after lookup error",
                "ts": "1710000000.0002575",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, {"text": "hello after lookup error"})

    async def test_bound_user_mismatched_dm_channel_missing_lookup_result_falls_back_to_processing(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}

        class _Store:
            def maybe_reload(self):
                return None

            def get_user(self, user_id, platform=None):
                if user_id == "U123":
                    return SimpleNamespace(dm_chat_id="D_STALE")
                return None

            def is_bound_user(self, user_id, platform=None):
                return user_id == "U123"

        class _SettingsManager:
            def get_store(self):
                return _Store()

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        class _WebClient:
            async def conversations_open(self, users):
                return {"ok": False, "error": "ratelimited"}

        async def _on_message(_context, text):
            received["text"] = text

        slack.web_client = _WebClient()
        slack.set_settings_manager(_SettingsManager())
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-dm-lookup-none",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "D_REAL",
                "user": "U123",
                "text": "hello after empty lookup",
                "ts": "1710000000.0002576",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, {"text": "hello after empty lookup"})

    async def test_bound_user_channel_message_is_not_blocked_by_dm_guard(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}

        class _Store:
            def maybe_reload(self):
                return None

            def get_user(self, user_id, platform=None):
                if user_id == "U123":
                    return SimpleNamespace(dm_chat_id="D_REAL")
                return None

            def find_channel(self, channel_id, platform=None):
                if channel_id == "C123":
                    return SimpleNamespace(enabled=True)
                return None

            def is_bound_user(self, user_id, platform=None):
                return user_id == "U123"

        class _SettingsManager:
            def get_store(self):
                return _Store()

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        async def _on_message(_context, text):
            received["text"] = text

        slack.set_settings_manager(_SettingsManager())
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-bound-user-channel-message",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U123",
                "text": "hello from channel",
                "ts": "1710000000.000258",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, {"text": "hello from channel"})

    async def test_bound_user_message_from_recorded_dm_channel_still_processes(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}

        class _Store:
            def maybe_reload(self):
                return None

            def get_user(self, user_id, platform=None):
                if user_id == "U123":
                    return SimpleNamespace(dm_chat_id="D_REAL")
                return None

            def is_bound_user(self, user_id, platform=None):
                return user_id == "U123"

        class _SettingsManager:
            def get_store(self):
                return _Store()

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        async def _on_message(_context, text):
            received["text"] = text

        slack.set_settings_manager(_SettingsManager())
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-dm-match",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "D_REAL",
                "user": "U123",
                "text": "hello",
                "ts": "1710000000.000260",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, {"text": "hello"})

    async def test_channel_mention_still_waits_for_app_mention(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {"called": False}

        async def _on_message(_context, _text):
            received["called"] = True

        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-channel-mention",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U123",
                "text": "<@U_BOT>",
                "ts": "1710000000.000300",
            },
        }

        await slack._handle_event(payload)

        self.assertFalse(received["called"])

    async def test_slack_connect_channel_mention_falls_back_to_message_event(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", require_mention=True))
        received = {}

        class _WebClient:
            async def conversations_info(self, channel):
                return _ResponseLike({"channel": {"id": channel, "is_ext_shared": True}})

        async def _on_message(context, text):
            received["text"] = text
            received["thread_id"] = context.thread_id
            received["bot_mention"] = (context.platform_specific or {}).get("bot_mention")

        slack.web_client = _WebClient()
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-slack-connect-mention",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C_CONNECT",
                "user": "U123",
                "text": "<@U_BOT> please help",
                "ts": "1710000000.000350",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(
            received,
            {
                "text": "<@U_BOT> please help",
                "thread_id": "1710000000.000350",
                "bot_mention": "<@U_BOT>",
            },
        )

    async def test_slack_connect_mention_only_falls_back_as_empty_message(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", require_mention=True))
        received = {}

        class _WebClient:
            async def conversations_info(self, channel):
                return {"channel": {"id": channel, "is_ext_shared": True}}

        async def _on_message(context, text):
            received["text"] = text
            received["thread_id"] = context.thread_id
            received["control_text"] = (context.platform_specific or {}).get("control_text")

        slack.web_client = _WebClient()
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-slack-connect-mention-only",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C_CONNECT",
                "user": "U123",
                "text": "<@U_BOT> \n",
                "ts": "1710000000.000355",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(
            received,
            {
                "text": "",
                "thread_id": "1710000000.000355",
                "control_text": "",
            },
        )

    async def test_slack_connect_mid_text_mention_is_forwarded_raw_in_message_fallback(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", require_mention=True))
        received = {}

        class _WebClient:
            async def conversations_info(self, channel):
                return {"channel": {"id": channel, "is_ext_shared": True}}

        async def _on_message(_context, text):
            received["text"] = text

        slack.web_client = _WebClient()
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-slack-connect-mid-text-mention",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C_CONNECT",
                "user": "U123",
                "text": "please <@U_BOT> help",
                "ts": "1710000000.000355",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, {"text": "please <@U_BOT> help"})

    async def test_slack_connect_other_bot_mention_is_forwarded_raw(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", require_mention=True))
        received = {}

        class _WebClient:
            async def conversations_info(self, channel):
                return {"channel": {"id": channel, "is_ext_shared": True}}

        async def _on_message(context, text):
            received["user_id"] = context.user_id
            received["text"] = text

        slack.web_client = _WebClient()
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-slack-connect-other-bot-mention",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C_CONNECT",
                "bot_id": "B_OTHER",
                "bot_profile": {"user_id": "U_OTHER_BOT"},
                "text": "handoff to <@U_BOT> please",
                "ts": "1710000000.000356",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, {"user_id": "U_OTHER_BOT", "text": "handoff to <@U_BOT> please"})

    async def test_own_bot_message_is_ignored_even_when_mentioned(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", app_id="A_SELF"))
        received = {"called": False}

        async def _on_message(_context, _text):
            received["called"] = True

        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-own-bot-mention",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C_CONNECT",
                "bot_id": "B_SELF",
                "bot_profile": {"user_id": "U_BOT", "app_id": "A_SELF"},
                "text": "<@U_BOT> loop",
                "ts": "1710000000.000357",
            },
        }

        await slack._handle_event(payload)

        self.assertFalse(received["called"])

    async def test_own_bot_message_with_top_level_app_id_is_ignored(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", app_id="A_SELF"))
        received = {"called": False}

        async def _on_message(_context, _text):
            received["called"] = True

        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-own-bot-top-level-app",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C_CONNECT",
                "bot_id": "B_SELF",
                "app_id": "A_SELF",
                "text": "<@U_BOT> loop",
                "ts": "1710000000.000358",
            },
        }

        await slack._handle_event(payload)

        self.assertFalse(received["called"])

    async def test_own_bot_message_with_auth_test_bot_id_is_ignored_without_app_id(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {"called": False}

        class _WebClient:
            async def auth_test(self):
                return {"user_id": "U_BOT", "bot_id": "B_SELF"}

        async def _on_message(_context, _text):
            received["called"] = True

        slack.web_client = _WebClient()
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-own-bot-auth-bot-id",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C_CONNECT",
                "bot_id": "B_SELF",
                "text": "<@U_BOT> loop",
                "ts": "1710000000.000359",
            },
        }

        await slack._handle_event(payload)

        self.assertFalse(received["called"])

    async def test_slack_connect_channel_mention_marks_thread_active(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", require_mention=True))
        marked = []

        class _WebClient:
            async def conversations_info(self, channel):
                return {"channel": {"id": channel, "is_ext_shared": True}}

        class _Sessions:
            def mark_thread_active(self, user_id, channel_id, thread_id):
                marked.append((user_id, channel_id, thread_id))

        class _SettingsManager:
            sessions = _Sessions()

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        async def _on_message(_context, _text):
            return None

        slack.web_client = _WebClient()
        slack.set_settings_manager(_SettingsManager())
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-slack-connect-mark-thread",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C_CONNECT",
                "user": "U123",
                "text": "<@U_BOT> please help",
                "ts": "1710000000.000360",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(marked, [("U123", "C_CONNECT", "1710000000.000360")])

    async def test_active_thread_requires_fresh_mention_when_require_mention_enabled(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", require_mention=True))
        received = []

        class _Sessions:
            def is_thread_active(self, _user_id, _channel_id, _thread_ts):
                return True

            def is_thread_active_for_user(self, _user_id, _channel_id, _thread_ts):
                return False

        class _SettingsManager:
            sessions = _Sessions()

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        async def _on_message(_context, text):
            received.append(text)

        slack.set_settings_manager(_SettingsManager())
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-active-thread-no-mention",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U123",
                "text": "@colleague take a look",
                "ts": "1710000000.000361",
                "thread_ts": "1710000000.000360",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, [])

    async def test_scheduled_active_thread_still_bypasses_mention_requirement(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", require_mention=True))
        received = []

        class _Sessions:
            def is_thread_active(self, _user_id, _channel_id, _thread_ts):
                return True

            def is_thread_active_for_user(self, user_id, channel_id, thread_ts):
                return user_id == "scheduled" and channel_id == "C123" and thread_ts == "1710000000.000360"

        class _SettingsManager:
            sessions = _Sessions()

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        async def _on_message(_context, text):
            received.append(text)

        slack.set_settings_manager(_SettingsManager())
        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-scheduled-thread-no-mention",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U123",
                "text": "scheduled follow-up context",
                "ts": "1710000000.000362",
                "thread_ts": "1710000000.000360",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, ["scheduled follow-up context"])

    async def test_socket_mode_events_ack_before_message_processing_finishes(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        started = asyncio.Event()
        release = asyncio.Event()
        received = []
        acks = []

        class _Client:
            async def send_socket_mode_response(self, response):
                acks.append(response)

        async def _on_message(_context, text):
            started.set()
            await release.wait()
            received.append(text)

        slack.register_callbacks(on_message=_on_message)
        payload = {
            "event_id": "evt-ack-before-processing",
            "team_id": "T1",
            "event": {
                "type": "message",
                "channel": "D123",
                "user": "U123",
                "text": "hello",
                "ts": "1710000000.000370",
            },
        }

        await slack._handle_socket_mode_request(
            _Client(),
            SimpleNamespace(type="events_api", envelope_id="env-1", payload=payload),
        )

        self.assertEqual(len(acks), 1)
        self.assertEqual(received, [])
        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        if slack._event_tasks:
            await asyncio.gather(*list(slack._event_tasks))
        self.assertEqual(received, ["hello"])

    async def test_slack_event_dedup_uses_persistent_runtime_claim(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        claims = set()
        calls = []
        received = []

        class _Sessions:
            def try_record_runtime_event(self, record_type, record_key, payload=None, *, ttl_seconds=None):
                calls.append((record_type, record_key, payload, ttl_seconds))
                key = (record_type, record_key)
                if key in claims:
                    return False
                claims.add(key)
                return True

        class _SettingsManager:
            sessions = _Sessions()

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        async def _on_message(_context, text):
            received.append(text)

        slack.set_settings_manager(_SettingsManager())
        slack.register_callbacks(on_message=_on_message)
        payload = {
            "event_id": "evt-persistent-dedup",
            "team_id": "T1",
            "event": {
                "type": "message",
                "channel": "D123",
                "user": "U123",
                "text": "hello",
                "ts": "1710000000.000371",
            },
        }

        await slack._handle_event(payload)
        await slack._handle_event(payload)

        self.assertEqual(received, ["hello"])
        self.assertEqual(calls[0][0], "slack_event")
        self.assertEqual(calls[0][1], "T1:evt-persistent-dedup")
        self.assertEqual(calls[0][3], 300)

    async def test_async_close_drains_pending_event_tasks_before_closing_web_client(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        started = asyncio.Event()
        release = asyncio.Event()
        events = []

        class _WebClient:
            async def close(self):
                events.append("web-close")

        async def _handler():
            events.append("handler-start")
            started.set()
            await release.wait()
            events.append("handler-finish")

        slack.web_client = _WebClient()
        task = asyncio.create_task(_handler())
        slack._event_tasks.add(task)
        task.add_done_callback(slack._handle_event_task_done)

        await asyncio.wait_for(started.wait(), timeout=1)
        close_task = asyncio.create_task(slack._async_close())
        await asyncio.sleep(0)
        self.assertEqual(events, ["handler-start"])

        release.set()
        await asyncio.wait_for(close_task, timeout=1)

        self.assertEqual(events, ["handler-start", "handler-finish", "web-close"])
        self.assertEqual(slack._event_tasks, set())

    async def test_async_close_cancels_event_tasks_after_drain_timeout(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        started = asyncio.Event()
        canceled = asyncio.Event()

        async def _handler():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                canceled.set()
                raise

        slack._event_task_shutdown_drain_timeout = 0.01
        task = asyncio.create_task(_handler())
        slack._event_tasks.add(task)
        task.add_done_callback(slack._handle_event_task_done)

        await asyncio.wait_for(started.wait(), timeout=1)
        await slack._async_close()

        await asyncio.wait_for(canceled.wait(), timeout=1)
        self.assertTrue(task.cancelled())
        self.assertEqual(slack._event_tasks, set())

    async def test_slack_connect_message_then_app_mention_is_handled_once(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", require_mention=True))
        received = []

        class _WebClient:
            async def conversations_info(self, channel):
                return {"channel": {"id": channel, "is_ext_shared": True}}

        async def _on_message(_context, text):
            received.append(text)

        slack.web_client = _WebClient()
        slack.register_callbacks(on_message=_on_message)

        message_payload = {
            "event_id": "evt-slack-connect-message-first",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C_CONNECT",
                "user": "U123",
                "text": "<@U_BOT> please help",
                "ts": "1710000000.000370",
            },
        }
        app_mention_payload = {
            "event_id": "evt-slack-connect-app-mention-second",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "app_mention",
                "channel": "C_CONNECT",
                "user": "U123",
                "text": "<@U_BOT> please help",
                "ts": "1710000000.000370",
            },
        }

        await slack._handle_event(message_payload)
        await slack._handle_event(app_mention_payload)

        self.assertEqual(received, ["<@U_BOT> please help"])

    async def test_slack_connect_app_mention_then_message_is_handled_once(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test", require_mention=True))
        received = []

        class _WebClient:
            async def conversations_info(self, channel):
                return {"channel": {"id": channel, "is_ext_shared": True}}

        async def _on_message(_context, text):
            received.append(text)

        slack.web_client = _WebClient()
        slack.register_callbacks(on_message=_on_message)

        app_mention_payload = {
            "event_id": "evt-slack-connect-app-mention-first",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "app_mention",
                "channel": "C_CONNECT",
                "user": "U123",
                "text": "<@U_BOT> please help",
                "ts": "1710000000.000380",
            },
        }
        message_payload = {
            "event_id": "evt-slack-connect-message-second",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C_CONNECT",
                "user": "U123",
                "text": "<@U_BOT> please help",
                "ts": "1710000000.000380",
            },
        }

        await slack._handle_event(app_mention_payload)
        await slack._handle_event(message_payload)

        self.assertEqual(received, ["<@U_BOT> please help"])

    async def test_dm_preserves_non_bot_mentions(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}

        async def _on_message(_context, text):
            received["text"] = text

        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-dm-mention-preserve-other",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "D123",
                "user": "U123",
                "text": "<@U_BOT> summarize what <@U_OTHER> said",
                "ts": "1710000000.000400",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, {"text": "<@U_BOT> summarize what <@U_OTHER> said"})

    async def test_channel_message_with_other_mentions_is_not_skipped(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}

        async def _on_message(_context, text):
            received["text"] = text

        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-channel-other-mention",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U123",
                "text": "please ask <@U_OTHER> to check",
                "ts": "1710000000.000500",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, {"text": "please ask <@U_OTHER> to check"})

    async def test_channel_message_with_bot_mention_mid_text_is_skipped(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {"called": False}

        async def _on_message(_context, _text):
            received["called"] = True

        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-channel-bot-mention-middle",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U123",
                "text": "hello <@U_BOT> please help",
                "ts": "1710000000.000550",
            },
        }

        await slack._handle_event(payload)

        self.assertFalse(received["called"])

    async def test_app_mention_preserves_non_bot_mentions(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}

        async def _on_message(_context, text):
            received["text"] = text

        slack.register_callbacks(on_message=_on_message)

        payload = {
            "event_id": "evt-app-mention-preserve-other",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U123",
                "text": "<@U_BOT> summarize what <@U_OTHER> said",
                "ts": "1710000000.000600",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(received, {"text": "<@U_BOT> summarize what <@U_OTHER> said"})


if __name__ == "__main__":
    unittest.main()
