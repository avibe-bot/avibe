"""Lark reaction ``emoji_type`` mappings.

Lark rejects an unknown ``emoji_type`` with a parameter error, and the keys are
**case-sensitive**: a plausible-looking ``FIRE`` or ``CROSSMARK`` fails exactly
like a typo, silently, because ``add_reaction`` only warns when the normalized
value is non-ASCII. Every expectation below was read off the published table
(https://open.feishu.cn/document/server-docs/im-v1/message-reaction/emojis-introduce)
rather than guessed from the unicode name.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.processing_indicator import (  # noqa: E402
    ACK_REACTION_EMOJI,
    NOT_DELIVERED_REACTION_EMOJI,
    QUEUED_REACTION_EMOJI,
    STEERED_REACTION_EMOJI,
    UNCONFIRMED_REACTION_EMOJI,
)
from core.handlers.message_handler import SUBAGENT_REACTION_EMOJI  # noqa: E402
from modules.im.feishu import _EMOJI_MAP, _normalize_emoji  # noqa: E402

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
        ):
            with self.subTest(emoji=emoji):
                self.assertIn(emoji, _EMOJI_MAP)
                self.assertTrue(_normalize_emoji(emoji).isascii())

    def test_variation_selector_forms_share_one_mapping(self):
        """``_normalize_emoji`` strips colons and whitespace, not U+FE0F."""

        self.assertEqual(_normalize_emoji("✍️"), _normalize_emoji("✍"))
        self.assertEqual(_normalize_emoji(":writing_hand:"), "Typing")
        self.assertEqual(_normalize_emoji(":shrug:"), "Shrug")


if __name__ == "__main__":
    unittest.main()
