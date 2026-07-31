"""Post-send bookkeeping must never destroy an already-delivered message id.

Every IM adapter send path does the same three things in order: hand the payload
to the platform transport, run local bookkeeping (``sessions.mark_thread_active``
for threaded targets), then return the native message id. The transport call is
the point of no return -- once it succeeds the user HAS the message.

If the bookkeeping step is allowed to raise, the exception replaces the return
value and reaches the caller's blanket handler
(``core/message_dispatcher.py`` notify/result send stages), which records a send
failure with NO delivery evidence. Anything that owes a durable notice for that
message then sees "never delivered" and re-sends it: a systematic duplicate on
every bookkeeping failure, not the crash-only at-least-once residual.

``modules/im/discord.py`` (``send_message`` / ``send_message_with_buttons``)
already guards the call with ``try/except Exception: pass``. These tests pin the
same invariant for Slack and Feishu, which were missed. Platform clients are
stubbed, so nothing touches the network.

Subordinate context: this is the ack/delivery lane that HFR-079's family covers
at the dispatcher/notice level; these cases are adapter-level unit pins and
introduce no new scenario id.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.v2_config import LarkConfig, SlackConfig
from modules.im.base import InlineButton, InlineKeyboard, MessageContext
from modules.im.feishu import FeishuBot
from modules.im.slack import SlackBot


class _Boom(RuntimeError):
    """Whatever the session store can raise (locked SQLite, IO error, ...)."""


def _raising_sessions() -> SimpleNamespace:
    return SimpleNamespace(mark_thread_active=Mock(side_effect=_Boom("session store unavailable")))


class SlackPostSendBookkeepingTests(unittest.IsolatedAsyncioTestCase):
    """Slack: every send path returning a ``ts`` survives a bookkeeping raise."""

    def _bot(self) -> SlackBot:
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        slack._ensure_clients = lambda: None  # type: ignore[method-assign]
        slack.settings_manager = object()
        slack.sessions = _raising_sessions()
        return slack

    @staticmethod
    def _threaded_context() -> MessageContext:
        return MessageContext(user_id="U1", channel_id="C1", thread_id="1710000000.000100", platform="slack")

    async def test_send_message_returns_ts_when_bookkeeping_raises(self):
        slack = self._bot()
        sent = []

        async def fake_prepared(context, text, parse_mode=None, reply_to=None):
            sent.append(text)
            return {"ts": "ts-plain"}

        slack._send_prepared_text_message = fake_prepared  # type: ignore[method-assign]

        message_id = await slack.send_message(self._threaded_context(), "delivered body")

        self.assertEqual(sent, ["delivered body"])
        self.assertEqual(message_id, "ts-plain")
        slack.sessions.mark_thread_active.assert_called_once()

    async def test_status_bubble_returns_ts_when_bookkeeping_raises(self):
        slack = self._bot()
        sent = []

        async def fake_post(context, kwargs, log_label=None):
            sent.append(kwargs)
            return {"ts": "ts-bubble"}

        slack._post_message_with_dm_recovery = fake_post  # type: ignore[method-assign]

        # A ``subtext`` routes send_message through _send_status_message.
        message_id = await slack.send_message(self._threaded_context(), "body", subtext="⏳ 5s")

        self.assertEqual(len(sent), 1)
        self.assertEqual(message_id, "ts-bubble")
        slack.sessions.mark_thread_active.assert_called_once()

    async def test_markdown_message_returns_ts_when_bookkeeping_raises(self):
        slack = self._bot()
        sent = []

        async def fake_post(context, kwargs, log_label=None):
            sent.append(kwargs)
            return {"ts": "ts-markdown"}

        slack._post_message_with_dm_recovery = fake_post  # type: ignore[method-assign]

        message_id = await slack.send_markdown_message(self._threaded_context(), "# Result")

        self.assertEqual(len(sent), 1)
        self.assertEqual(message_id, "ts-markdown")
        slack.sessions.mark_thread_active.assert_called_once()

    async def test_message_with_buttons_returns_ts_when_bookkeeping_raises(self):
        slack = self._bot()
        sent = []

        async def fake_post(context, kwargs, log_label=None):
            sent.append(kwargs)
            return {"ts": "ts-buttons"}

        slack._post_message_with_dm_recovery = fake_post  # type: ignore[method-assign]
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="Stop", callback_data="stop")]])

        message_id = await slack.send_message_with_buttons(self._threaded_context(), "pick one", keyboard)

        self.assertEqual(len(sent), 1)
        self.assertEqual(message_id, "ts-buttons")
        slack.sessions.mark_thread_active.assert_called_once()


class _FakeLarkResponse:
    def __init__(self, message_id: str) -> None:
        self.data = types.SimpleNamespace(message_id=message_id, chat_id="oc_chat")
        self.code = 0
        self.msg = "ok"

    def success(self) -> bool:
        return True


class FeishuPostSendBookkeepingTests(unittest.IsolatedAsyncioTestCase):
    """Feishu: the threaded reply paths survive a bookkeeping raise.

    Only the ``root_id`` branches are covered because the trailing
    ``mark_thread_active`` blocks in ``send_message`` / ``send_message_with_buttons``
    sit after the early ``return`` taken whenever a thread target exists, so no
    caller can reach them; they are wrapped for parity, not for a live path.
    """

    def _bot(self, message_id: str) -> tuple[FeishuBot, types.SimpleNamespace]:
        bot = FeishuBot(LarkConfig(app_id="app-id", app_secret="app-secret"))
        bot._ensure_client = lambda: None  # type: ignore[method-assign]
        message = types.SimpleNamespace(
            acreate=AsyncMock(return_value=_FakeLarkResponse(message_id)),
            areply=AsyncMock(return_value=_FakeLarkResponse(message_id)),
        )
        bot._lark_client = types.SimpleNamespace(
            im=types.SimpleNamespace(v1=types.SimpleNamespace(message=message))
        )
        bot.settings_manager = object()
        bot.sessions = _raising_sessions()
        return bot, message

    @staticmethod
    def _threaded_context() -> MessageContext:
        return MessageContext(user_id="ou_1", channel_id="oc_chat", thread_id="om_root", platform="lark")

    async def test_thread_reply_returns_message_id_when_bookkeeping_raises(self):
        bot, message = self._bot("om_reply")

        result = await bot.send_message(self._threaded_context(), "delivered body")

        message.areply.assert_awaited_once()
        self.assertEqual(result, "om_reply")
        bot.sessions.mark_thread_active.assert_called_once()

    async def test_thread_card_reply_returns_message_id_when_bookkeeping_raises(self):
        bot, message = self._bot("om_card")
        keyboard = InlineKeyboard(buttons=[[InlineButton(text="Stop", callback_data="stop")]])

        result = await bot.send_message_with_buttons(self._threaded_context(), "pick one", keyboard)

        message.areply.assert_awaited_once()
        self.assertEqual(result, "om_card")
        bot.sessions.mark_thread_active.assert_called_once()


if __name__ == "__main__":
    unittest.main()
