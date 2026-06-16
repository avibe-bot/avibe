from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.im import MessageContext
from modules.im.discord import DiscordBot
from core.auth import AuthResult


class _FakeSessions:
    def has_any_agent_session_base(self, user_id, base_session_id):
        return user_id == "discord::C123" and base_session_id == "discord_555"

    def is_thread_active(self, user_id, channel_id, thread_ts):
        return user_id == "scheduled" and channel_id == "C123" and thread_ts == "777"


class _FakeChannel:
    async def fetch_message(self, message_id):
        return SimpleNamespace(id=message_id, thread=SimpleNamespace(id=777))


class DiscordReplyAnchorTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_turn_context_uses_reply_anchor_thread_when_known_session_exists(self):
        bot = object.__new__(DiscordBot)
        bot.sessions = _FakeSessions()
        bot._loop = None

        async def _fetch_channel(channel_id):
            self.assertEqual(channel_id, "C123")
            return _FakeChannel()

        async def _maybe_create_thread(message):
            raise AssertionError("existing thread should be reused")

        bot._fetch_channel = _fetch_channel
        bot._maybe_create_thread = _maybe_create_thread

        message = SimpleNamespace(guild=object(), reference=SimpleNamespace(message_id=555))
        context = MessageContext(
            user_id="U123",
            channel_id="C123",
            platform="discord",
            message_id="999",
            platform_specific={"message": message, "is_dm": False},
        )

        prepared = await DiscordBot.prepare_turn_context(bot, context, "human")

        self.assertEqual(prepared.thread_id, "777")
        self.assertEqual(prepared.platform_specific["reply_anchor_base_session_id"], "discord_555")
        self.assertEqual(prepared.platform_specific["reply_anchor_message_id"], "555")

    def test_scheduled_thread_activity_allows_replies_under_mention_gating(self):
        bot = object.__new__(DiscordBot)
        bot.sessions = _FakeSessions()
        bot.settings_manager = object()

        allowed = DiscordBot._is_thread_reply_allowed(bot, "U123", "C123", "777")

        self.assertTrue(allowed)

    async def test_send_auth_denial_acknowledges_silent_interaction_denial(self):
        bot = object.__new__(DiscordBot)
        bot.build_auth_denial_text = lambda denial, channel_id=None: None
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                is_done=lambda: False,
                defer=AsyncMock(),
            )
        )

        await DiscordBot._send_auth_denial(
            bot,
            "C123",
            "U-disabled",
            AuthResult(allowed=False, denial="not_bound_channel"),
            interaction=interaction,
        )

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
