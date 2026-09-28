"""Where Discord puts a turn's typing indicator and its 👌/👀 reactions.

Typing and reactions describe one turn in one thread, so an unresolvable thread
(deleted, no access) yields nothing instead of surfacing them in the parent
channel. A reaction goes on the message where it was posted: a thread opened
from a message shares that message's id, and the message lives in the parent.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import modules.im.discord as discord_module
from modules.im import MessageContext
from modules.im.discord import DiscordBot


class _Message:
    def __init__(self, channel, message_id):
        self.channel = channel
        self.id = message_id

    async def add_reaction(self, emoji):
        self.channel.calls.append(("add", self.id, emoji))

    async def remove_reaction(self, emoji, _user):
        self.channel.calls.append(("remove", self.id, emoji))


class _Channel:
    def __init__(self, channel_id, *, messages=()):
        self.id = channel_id
        self.messages = {str(message_id) for message_id in messages}
        self.calls: list[tuple] = []

    async def typing(self):
        self.calls.append(("typing",))

    async def fetch_message(self, message_id):
        if str(message_id) not in self.messages:
            raise LookupError(f"Unknown Message {message_id} in {self.id}")
        return _Message(self, str(message_id))


class _TextChannel(_Channel):
    pass


class _ForumChannel(_Channel):
    pass


class _Thread(_Channel):
    def __init__(self, thread_id, *, parent, messages=(), cached_parent=True):
        super().__init__(thread_id, messages=messages)
        self.parent = parent if cached_parent else None
        self.parent_id = parent.id


def _bot(channels):
    bot = object.__new__(DiscordBot)
    bot._loop = None
    bot.client = SimpleNamespace(user=SimpleNamespace(id=42))

    async def _fetch_channel(channel_id):
        return channels.get(str(channel_id))

    bot._fetch_channel = _fetch_channel
    return bot


def _context(*, thread_id, message_id, message=None):
    return MessageContext(
        user_id="U1",
        channel_id="100",
        platform="discord",
        thread_id=thread_id,
        message_id=message_id,
        platform_specific={"message": message} if message is not None else {},
    )


class DiscordIndicatorTargetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.multiple(
            discord_module.discord,
            Thread=_Thread,
            TextChannel=_TextChannel,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_unresolvable_thread_gives_no_typing_or_reaction_in_parent(self):
        # The thread was deleted after the turn started; its id no longer
        # resolves, but the parent channel does and holds a message of that id.
        parent = _TextChannel("100", messages=["555", "200"])
        bot = _bot({"100": parent})
        for message_id in ("555", "200"):
            with self.subTest(message_id=message_id):
                context = _context(thread_id="555", message_id=message_id)

                self.assertFalse(await bot.send_typing_indicator(context))
                self.assertFalse(await bot.add_reaction(context, message_id, "👌"))
                self.assertFalse(await bot.add_reaction(context, message_id, "👀"))
                self.assertFalse(await bot.remove_reaction(context, message_id, "👀"))
                self.assertEqual(parent.calls, [])

    async def test_starter_message_reaction_is_applied_through_its_text_parent(self):
        # A restored or re-dispatched context has no live message object; the
        # starter is identified by sharing the thread's id.
        for cached_parent in (True, False):
            with self.subTest(cached_parent=cached_parent):
                parent = _TextChannel("100", messages=["555"])
                thread = _Thread("555", parent=parent, cached_parent=cached_parent)
                bot = _bot({"100": parent, "555": thread})
                context = _context(thread_id="555", message_id="555")

                self.assertTrue(await bot.add_reaction(context, "555", "👌"))
                self.assertTrue(await bot.remove_reaction(context, "555", "👌"))
                self.assertTrue(await bot.add_reaction(context, "555", "👀"))
                self.assertTrue(await bot.remove_reaction(context, "555", "👀"))

                self.assertEqual(
                    parent.calls,
                    [("add", "555", "👌"), ("remove", "555", "👌"), ("add", "555", "👀"), ("remove", "555", "👀")],
                )
                self.assertEqual(thread.calls, [])
                # Typing still belongs to the thread the turn replies in.
                self.assertTrue(await bot.send_typing_indicator(context))
                self.assertEqual(thread.calls, [("typing",)])

    async def test_forum_starter_reaction_stays_in_its_thread(self):
        forum = _ForumChannel("100")
        thread = _Thread("555", parent=forum, messages=["555"])
        bot = _bot({"100": forum, "555": thread})
        context = _context(thread_id="555", message_id="555")

        self.assertTrue(await bot.add_reaction(context, "555", "👀"))
        self.assertEqual(thread.calls, [("add", "555", "👀")])
        self.assertEqual(forum.calls, [])

    async def test_live_message_reaction_uses_the_channel_it_was_posted_in(self):
        # A channel reply to an anchor runs in the anchor's thread, but the
        # user's own message stays in the channel.
        parent = _TextChannel("100", messages=["999"])
        thread = _Thread("555", parent=parent)
        bot = _bot({"100": parent, "555": thread})
        message = SimpleNamespace(id=999, channel=parent)
        context = _context(thread_id="555", message_id="999", message=message)

        self.assertTrue(await bot.add_reaction(context, "999", "👀"))
        self.assertTrue(await bot.remove_reaction(context, "999", "👀"))
        self.assertEqual(parent.calls, [("add", "999", "👀"), ("remove", "999", "👀")])
        self.assertEqual(thread.calls, [])


if __name__ == "__main__":
    unittest.main()
