from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import DiscordConfig
from modules.im import MessageContext
from modules.im.discord import DiscordBot
from core.auth import AuthResult


class _FakeSessions:
    def has_any_agent_session_base(self, user_id, base_session_id):
        return user_id == "discord::C123" and base_session_id == "discord_555"

    def is_thread_active(self, user_id, channel_id, thread_ts):
        return user_id == "scheduled" and channel_id == "C123" and thread_ts == "777"

    def is_thread_active_for_user(self, user_id, channel_id, thread_ts):
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

    def test_scheduled_thread_activity_is_checked_with_exact_owner(self):
        bot = object.__new__(DiscordBot)
        bot.sessions = _FakeSessions()
        bot.settings_manager = object()

        self.assertTrue(DiscordBot.is_scheduled_thread_active(bot, "C123", "777"))

    def test_human_active_thread_does_not_count_as_scheduled_activity(self):
        bot = object.__new__(DiscordBot)

        class _Sessions:
            def is_thread_active(self, _user_id, _channel_id, _thread_id):
                return True

            def is_thread_active_for_user(self, _user_id, _channel_id, _thread_id):
                return False

        bot.sessions = _Sessions()
        bot.settings_manager = object()

        self.assertFalse(DiscordBot.is_scheduled_thread_active(bot, "C123", "777"))

    async def test_human_active_thread_requires_fresh_mention_when_require_mention_enabled(self):
        import modules.im.discord as discord_module

        bot = object.__new__(DiscordBot)
        bot.config = DiscordConfig(require_mention=True)
        bot._controller = None
        bot.client = SimpleNamespace(user=SimpleNamespace(id=42))
        bot.on_message_callback = AsyncMock()

        class _Sessions:
            def is_thread_active(self, _user_id, _channel_id, _thread_id):
                return True

            def is_thread_active_for_user(self, _user_id, _channel_id, _thread_id):
                return False

        class _SettingsManager:
            sessions = _Sessions()
            platform = "discord"

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        bot.sessions = _Sessions()
        bot.settings_manager = _SettingsManager()
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True, is_dm=False)
        bot.dispatch_text_command = AsyncMock(return_value=False)

        original_thread = discord_module.discord.Thread

        class _FakeThread:
            id = 777
            parent_id = "C123"

        try:
            discord_module.discord.Thread = _FakeThread
            message = SimpleNamespace(
                author=SimpleNamespace(id=456, bot=False),
                content="@colleague take a look",
                channel=_FakeThread(),
                guild=SimpleNamespace(id=999),
                attachments=[],
                mentions=[SimpleNamespace(id=9999)],
                id=888,
            )

            await DiscordBot._on_message_event(bot, message)
        finally:
            discord_module.discord.Thread = original_thread

        bot.on_message_callback.assert_not_awaited()

    async def test_scheduled_active_thread_bypasses_mention_requirement(self):
        import modules.im.discord as discord_module

        bot = object.__new__(DiscordBot)
        bot.config = DiscordConfig(require_mention=True)
        bot._controller = None
        bot.client = SimpleNamespace(user=SimpleNamespace(id=42))
        bot.on_message_callback = AsyncMock()
        bot.sessions = _FakeSessions()

        class _SettingsManager:
            sessions = _FakeSessions()
            platform = "discord"

            def get_require_mention(self, _channel_id, global_default=False):
                return global_default

        bot.settings_manager = _SettingsManager()
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True, is_dm=False)
        bot.dispatch_text_command = AsyncMock(return_value=False)

        original_thread = discord_module.discord.Thread

        class _FakeThread:
            id = 777
            parent_id = "C123"

        try:
            discord_module.discord.Thread = _FakeThread
            message = SimpleNamespace(
                author=SimpleNamespace(id=456, bot=False),
                content="scheduled follow-up context",
                channel=_FakeThread(),
                guild=SimpleNamespace(id=999),
                attachments=[],
                mentions=[],
                id=888,
            )

            await DiscordBot._on_message_event(bot, message)
        finally:
            discord_module.discord.Thread = original_thread

        bot.on_message_callback.assert_awaited_once()
        self.assertEqual(bot.on_message_callback.await_args.args[1], "scheduled follow-up context")

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
