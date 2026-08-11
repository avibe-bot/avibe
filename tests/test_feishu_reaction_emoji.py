"""Lark reaction ``emoji_type`` mappings.

Lark rejects an unknown ``emoji_type`` with a parameter error, and the keys are
**case-sensitive**: a plausible-looking ``FIRE`` or ``CROSSMARK`` fails exactly
like a typo, silently, because ``add_reaction`` only warns when the normalized
value is non-ASCII. Every expectation below was read off the published table
(https://open.feishu.cn/document/server-docs/im-v1/message-reaction/emojis-introduce)
rather than guessed from the unicode name. That page renders its table
client-side, so where it could not be read directly the key was confirmed
against the ``chyroc/lark`` ``type_emoji.go`` enum, in which every key carries
the official ``lark-reaction-cn/emoji_*.png`` CDN image it renders as — enough
to verify a key exists and what it depicts. Absence from that enum proves
nothing (it omits ``Shrug`` and ``GoGoGo``, both of which work), so it is only
ever used as evidence *for* a key.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.processing_indicator import (  # noqa: E402
    ACK_REACTION_EMOJI,
    INTERRUPTED_REACTION_EMOJI,
    NOT_DELIVERED_REACTION_EMOJI,
    QUEUED_REACTION_EMOJI,
    STEERED_REACTION_EMOJI,
    STOPPED_REACTION_EMOJI,
    UNCONFIRMED_REACTION_EMOJI,
)
from core.handlers.message_handler import SUBAGENT_REACTION_EMOJI  # noqa: E402
from modules.im.feishu import FeishuBot, _EMOJI_MAP, _normalize_emoji  # noqa: E402

# unicode emoji -> the exact emoji_type Lark publishes for it.
VERIFIED_EMOJI_TYPES = {
    "👀": "OnIt",
    "🤖": "SMART",
    "👌": "OK",
    "✍️": "Typing",
    "✍": "Typing",
    "🤷": "Shrug",
    "🤔": "THINKING",
    "👍": "THUMBSUP",
    "👎": "ThumbsDown",
    "❤️": "HEART",
    "✅": "OK",
    "❌": "CrossMark",
    "🚀": "GoGoGo",
    "😄": "SMILE",
    "🔥": "Fire",
    "👏": "APPLAUSE",
    "💪": "MUSCLE",
    "🎉": "PARTY",
    # Terminal receipts. Lark publishes no ⏹️/⚠️ glyph; these two keys are the
    # closest published meanings (emoji_silent.png / emoji_errr.png).
    "⏹️": "SILENT",
    "⏹": "SILENT",
    "⚠️": "ERROR",
    "⚠": "ERROR",
}


class FeishuReactionEmojiTests(unittest.TestCase):
    def test_normalizes_to_published_emoji_types(self):
        for emoji, emoji_type in VERIFIED_EMOJI_TYPES.items():
            with self.subTest(emoji=emoji):
                self.assertEqual(_normalize_emoji(emoji), emoji_type)

    def test_every_reaction_the_agent_sends_is_mapped(self):
        """An unmapped receipt degrades to no reaction at all on Lark.

        ``_normalize_emoji`` falls through to ``.upper()``, so a missing entry
        ships the raw codepoint as the ``emoji_type`` and the user simply never
        sees the receipt. Pin the set the agent actually sends.
        """

        for emoji in (
            ACK_REACTION_EMOJI,
            QUEUED_REACTION_EMOJI,
            STEERED_REACTION_EMOJI,
            UNCONFIRMED_REACTION_EMOJI,
            NOT_DELIVERED_REACTION_EMOJI,
            SUBAGENT_REACTION_EMOJI,
            STOPPED_REACTION_EMOJI,
            INTERRUPTED_REACTION_EMOJI,
        ):
            with self.subTest(emoji=emoji):
                self.assertIn(emoji, _EMOJI_MAP)
                self.assertTrue(_normalize_emoji(emoji).isascii())

    def test_terminal_receipts_stay_distinguishable(self):
        """A stop and a crash must not land on the same reaction.

        ⏹️ is the only trace a ``/stop`` leaves — its result is deliberately
        silent — so collapsing it onto the crash key would make a clean stop
        look like a dead runtime.
        """

        self.assertNotEqual(
            _normalize_emoji(STOPPED_REACTION_EMOJI),
            _normalize_emoji(INTERRUPTED_REACTION_EMOJI),
        )

    def test_variation_selector_forms_share_one_mapping(self):
        """``_normalize_emoji`` strips colons and whitespace, not U+FE0F."""

        self.assertEqual(_normalize_emoji("✍️"), _normalize_emoji("✍"))
        self.assertEqual(_normalize_emoji(":writing_hand:"), "Typing")
        self.assertEqual(_normalize_emoji(":shrug:"), "Shrug")

    def test_reaction_cleanup_accepts_only_this_app_operator(self):
        bot = FeishuBot.__new__(FeishuBot)
        bot.config = SimpleNamespace(app_id="cli_app")
        bot._bot_open_id = "ou_bot"

        self.assertTrue(bot._reaction_is_owned_by_bot({"operator": {"operator_type": "app", "operator_id": "ou_bot"}}))
        self.assertTrue(bot._reaction_is_owned_by_bot({"operator": {"operator_type": "app", "operator_id": "cli_app"}}))
        self.assertFalse(
            bot._reaction_is_owned_by_bot({"operator": {"operator_type": "user", "operator_id": "ou_user"}})
        )
        self.assertFalse(
            bot._reaction_is_owned_by_bot({"operator": {"operator_type": "app", "operator_id": "cli_other"}})
        )


class FeishuReactionCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_remove_skips_human_match_and_finds_bot_on_next_page(self):
        pages = [
            {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "reaction_id": "human-reaction",
                            "operator": {"operator_type": "user", "operator_id": "ou_user"},
                            "reaction_type": {"emoji_type": "OnIt"},
                        }
                    ],
                    "has_more": True,
                    "page_token": "next-page",
                },
            },
            {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "reaction_id": "bot-reaction",
                            "operator": {"operator_type": "app", "operator_id": "ou_bot"},
                            "reaction_type": {"emoji_type": "OnIt"},
                        }
                    ],
                    "has_more": False,
                },
            },
        ]
        get_params = []
        deleted = []

        class _Response:
            def __init__(self, *, payload=None, status=200):
                self.payload = payload
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self):
                return self.payload

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, _url, *, headers, params):
                get_params.append(dict(params))
                return _Response(payload=pages.pop(0))

            def delete(self, url, *, headers):
                deleted.append(url)
                return _Response(status=200)

        bot = FeishuBot.__new__(FeishuBot)
        bot.config = SimpleNamespace(app_id="cli_app", api_base_url="https://open.feishu.cn")
        bot._bot_open_id = "ou_bot"
        bot._lark_client = object()
        bot._get_tenant_token = AsyncMock(return_value="token")

        with patch("modules.im.feishu.aiohttp.ClientSession", return_value=_Session()):
            removed = await bot.remove_reaction(SimpleNamespace(), "m1", "👀")

        self.assertTrue(removed)
        self.assertNotIn("human-reaction", "".join(deleted))
        self.assertIn("bot-reaction", deleted[0])
        self.assertEqual(get_params[1]["page_token"], "next-page")


if __name__ == "__main__":
    unittest.main()
