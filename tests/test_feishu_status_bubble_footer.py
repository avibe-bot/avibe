"""Feishu concise status-bubble footer rendering (parity with Slack/Discord).

The dispatcher hands adapters ``(body, footer)`` separately via the optional
``subtext`` parameter. Card schema 2.0 dropped the ``note`` component, so Feishu
renders the footer as a trailing notation-sized grey ``markdown`` element (its
documented replacement) — the analog of Slack's context block and Discord's
``-#``. These tests cover:

- the pure card builder (``_build_card_json``): footer element iff ``subtext``
  is set, ``subtext=None`` is byte-identical to before, and footer-only (empty
  body) renders the footer alone;
- the send/edit/buttons paths forward ``subtext`` into the card;
- the empty-body guard relaxation;
- the lark platform capability flag.

The send paths use a stubbed lark client so nothing touches the network.
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.v2_config import LarkConfig
from modules.im import MessageContext
from modules.im.base import InlineButton, InlineKeyboard
from modules.im.feishu import FeishuBot


def _card_elements(card_json: str) -> list:
    return json.loads(card_json)["body"]["elements"]


def _footer_element(subtext: str) -> dict:
    # Card schema 2.0 has no ``note``; the footer is a notation-sized markdown
    # element with an inline grey font wrap (verified valid against the live
    # Feishu API — ``text_color`` as a property is rejected).
    return {"tag": "markdown", "content": f"<font color='grey'>{subtext}</font>", "text_size": "notation"}


def _assert_is_footer(test, element: dict, subtext: str) -> None:
    test.assertEqual(element, _footer_element(subtext))


def _make_bot() -> FeishuBot:
    return FeishuBot(LarkConfig(app_id="app-id", app_secret="app-secret"))


class _FakeResponse:
    def __init__(self, message_id: str = "om_new") -> None:
        self.data = types.SimpleNamespace(message_id=message_id, chat_id="oc_chat")
        self.code = 0
        self.msg = "ok"

    def success(self) -> bool:
        return True


def _stub_lark_client(bot: FeishuBot) -> types.SimpleNamespace:
    """Give ``bot`` a fake ``_lark_client`` capturing create/patch/reply calls."""
    message = types.SimpleNamespace(
        acreate=AsyncMock(return_value=_FakeResponse()),
        apatch=AsyncMock(return_value=_FakeResponse()),
        areply=AsyncMock(return_value=_FakeResponse()),
        adelete=AsyncMock(return_value=_FakeResponse()),
    )
    client = types.SimpleNamespace(im=types.SimpleNamespace(v1=types.SimpleNamespace(message=message)))
    bot._lark_client = client
    bot._ensure_client = lambda: None  # type: ignore[method-assign]
    return message


class BuildCardJsonFooterTests(unittest.TestCase):
    def test_subtext_appends_footer_element(self):
        bot = _make_bot()
        elements = _card_elements(bot._build_card_json("🔧 Read: feishu.py", subtext="⏳ 5s"))
        # Body markdown first, footer last.
        self.assertEqual(elements[0], {"tag": "markdown", "content": "🔧 Read: feishu.py"})
        _assert_is_footer(self, elements[-1], "⏳ 5s")

    def test_no_subtext_is_byte_identical_to_before(self):
        bot = _make_bot()
        # The pre-change output was a single markdown element and no footer.
        self.assertEqual(
            bot._build_card_json("hello"),
            json.dumps(
                {
                    "schema": "2.0",
                    "body": {"direction": "vertical", "elements": [{"tag": "markdown", "content": "hello"}]},
                },
                ensure_ascii=False,
            ),
        )
        # Explicit subtext=None is identical too.
        self.assertEqual(bot._build_card_json("hello"), bot._build_card_json("hello", subtext=None))

    def test_empty_text_without_subtext_keeps_empty_body_element(self):
        # The settings-toggle keyboard-only edit calls _build_card_json("",
        # buttons, subtext=None); it must keep the (empty) markdown element so the
        # card stays byte-for-byte identical to before this change.
        bot = _make_bot()
        buttons = [[{"text": "Toggle", "callback_data": "t"}]]
        elements = _card_elements(bot._build_card_json("", buttons, subtext=None))
        self.assertEqual(elements[0], {"tag": "markdown", "content": ""})
        self.assertEqual([e["tag"] for e in elements], ["markdown", "button"])

    def test_footer_only_empty_body_renders_footer_alone(self):
        bot = _make_bot()
        elements = _card_elements(bot._build_card_json("", subtext="⏳ working · 0s"))
        self.assertEqual(len(elements), 1)
        _assert_is_footer(self, elements[0], "⏳ working · 0s")

    def test_buttons_and_subtext_coexist(self):
        bot = _make_bot()
        buttons = [[{"text": "OK", "callback_data": "ok"}]]
        elements = _card_elements(bot._build_card_json("Final answer", buttons, subtext="✅ done · 248k tok"))
        tags = [e["tag"] for e in elements]
        self.assertEqual(tags[0], "markdown")
        self.assertIn("button", tags)
        # Footer is last so it stays below the buttons.
        _assert_is_footer(self, elements[-1], "✅ done · 248k tok")


class SendEditForwardsSubtextTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_forwards_subtext(self):
        bot = _make_bot()
        message = _stub_lark_client(bot)
        ctx = MessageContext(user_id="U1", channel_id="oc_chat", platform="lark")
        await bot.send_message(ctx, "🔧 x", subtext="⏳ 5s")
        content = json.loads(message.acreate.await_args.args[0].request_body.content)
        _assert_is_footer(self, content["body"]["elements"][-1], "⏳ 5s")

    async def test_send_message_footer_only_is_allowed(self):
        bot = _make_bot()
        message = _stub_lark_client(bot)
        ctx = MessageContext(user_id="U1", channel_id="oc_chat", platform="lark")
        # Empty body + subtext must NOT raise (turn-start footer-only bubble).
        mid = await bot.send_message(ctx, "", subtext="⏳ working · 0s")
        self.assertEqual(mid, "om_new")
        content = json.loads(message.acreate.await_args.args[0].request_body.content)
        self.assertEqual(len(content["body"]["elements"]), 1)
        _assert_is_footer(self, content["body"]["elements"][0], "⏳ working · 0s")

    async def test_send_message_empty_without_subtext_still_rejected(self):
        bot = _make_bot()
        _stub_lark_client(bot)
        ctx = MessageContext(user_id="U1", channel_id="oc_chat", platform="lark")
        with self.assertRaises(ValueError):
            await bot.send_message(ctx, "", subtext=None)

    async def test_thread_reply_forwards_subtext(self):
        bot = _make_bot()
        message = _stub_lark_client(bot)
        ctx = MessageContext(user_id="U1", channel_id="oc_chat", platform="lark", thread_id="om_root")
        await bot.send_message(ctx, "🔧 x", subtext="⏳ 5s")
        # Thread reply goes through areply (reply_in_thread), not acreate.
        message.acreate.assert_not_awaited()
        content = json.loads(message.areply.await_args.args[0].request_body.content)
        _assert_is_footer(self, content["body"]["elements"][-1], "⏳ 5s")

    async def test_edit_message_forwards_subtext(self):
        bot = _make_bot()
        message = _stub_lark_client(bot)
        ctx = MessageContext(user_id="U1", channel_id="oc_chat", platform="lark")
        ok = await bot.edit_message(ctx, "om_1", text="🔧 y", subtext="⏳ 9s")
        self.assertTrue(ok)
        content = json.loads(message.apatch.await_args.args[0].request_body.content)
        _assert_is_footer(self, content["body"]["elements"][-1], "⏳ 9s")

    async def test_edit_message_collapse_marker_footer_only(self):
        bot = _make_bot()
        message = _stub_lark_client(bot)
        ctx = MessageContext(user_id="U1", channel_id="oc_chat", platform="lark")
        # Terminal collapse edits body to "" and carries a marker footer.
        ok = await bot.edit_message(ctx, "om_1", text="", subtext="✅ done")
        self.assertTrue(ok)
        content = json.loads(message.apatch.await_args.args[0].request_body.content)
        self.assertEqual(len(content["body"]["elements"]), 1)
        _assert_is_footer(self, content["body"]["elements"][0], "✅ done")

    async def test_send_message_with_buttons_forwards_subtext(self):
        bot = _make_bot()
        message = _stub_lark_client(bot)
        ctx = MessageContext(user_id="U1", channel_id="oc_chat", platform="lark")
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="Continue", callback_data="cont")]])
        # Concise result path: buttons AND a done footer together.
        await bot.send_message_with_buttons(ctx, "Final answer", keyboard, subtext="✅ done · 248k tok")
        content = json.loads(message.acreate.await_args.args[0].request_body.content)
        tags = [e["tag"] for e in content["body"]["elements"]]
        # Both the buttons and the footer survive (the regression codex flagged).
        self.assertIn("button", tags)
        _assert_is_footer(self, content["body"]["elements"][-1], "✅ done · 248k tok")


    async def test_delete_message_calls_adelete(self):
        bot = _make_bot()
        message = _stub_lark_client(bot)
        ctx = MessageContext(user_id="U1", channel_id="oc_chat", platform="lark")
        ok = await bot.delete_message(ctx, "om_del")
        self.assertTrue(ok)
        self.assertEqual(message.adelete.await_args.args[0].message_id, "om_del")

    async def test_delete_message_returns_false_on_api_failure(self):
        bot = _make_bot()
        message = _stub_lark_client(bot)
        message.adelete.return_value = _FakeResponse()
        message.adelete.return_value.success = lambda: False  # type: ignore[assignment]
        ctx = MessageContext(user_id="U1", channel_id="oc_chat", platform="lark")
        # Falsey response → dispatcher falls back to collapsing the bubble.
        self.assertFalse(await bot.delete_message(ctx, "om_del"))


class LarkCapabilityTests(unittest.TestCase):
    def test_lark_advertises_status_bubble(self):
        from config.platform_registry import get_platform_descriptor

        self.assertTrue(get_platform_descriptor("lark").capabilities.supports_status_bubble)

    def test_lark_advertises_deletion(self):
        # Feishu can recall its own messages, so the concise bubble is deleted at
        # turn end (single-message finalize, matching Slack/Discord).
        from config.platform_registry import get_platform_descriptor

        self.assertTrue(get_platform_descriptor("lark").capabilities.supports_message_deletion)


if __name__ == "__main__":
    unittest.main()
