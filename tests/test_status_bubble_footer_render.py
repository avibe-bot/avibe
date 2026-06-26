"""Adapter-level rendering of the concise status-bubble footer (Step F).

The dispatcher now hands the IM adapters ``(body, footer)`` separately via the
optional ``subtext`` parameter; each adapter owns the footer styling:
- Slack renders the body as a native ``markdown`` block and the footer as a
  native ``context`` block (small gray text);
- Discord renders it as ``-#`` subtext.

These are pure structural unit tests that do not touch a live client.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.im import MessageContext
from modules.im.discord import DiscordBot
from modules.im.slack import SlackBot


class SlackStatusBlocksTests(unittest.TestCase):
    def test_build_status_blocks_markdown_body_plus_context(self):
        # Construct without running the heavy __init__; only the pure block
        # builder is exercised. parse_mode="plain" avoids the markdown converter.
        bot = SlackBot.__new__(SlackBot)
        blocks, fallback_text = bot._build_status_blocks(
            body="🔧 x", subtext="⏳ 5s", keyboard=None, parse_mode="plain"
        )
        # Body renders as a native markdown block (standard markdown, no
        # Show more/less auto-collapse) carrying the RAW body.
        self.assertEqual(blocks[0]["type"], "markdown")
        self.assertEqual(blocks[0]["text"], "🔧 x")
        # Footer stays a native context block (small gray text).
        self.assertEqual(blocks[1]["type"], "context")
        self.assertEqual(blocks[1]["elements"][0]["type"], "mrkdwn")
        self.assertEqual(blocks[1]["elements"][0]["text"], "⏳ 5s")
        # Fallback text (notifications / no-block clients) mirrors
        # send_markdown_message's visible-text fallback.
        self.assertEqual(fallback_text, "🔧 x")
        # No keyboard → exactly two blocks (no actions block).
        self.assertEqual(len(blocks), 2)

    def test_build_status_blocks_empty_body_is_context_only(self):
        # A footer-only bubble (no action label yet) drops the markdown block and
        # renders the footer context block alone; fallback text = stripped footer.
        bot = SlackBot.__new__(SlackBot)
        blocks, fallback_text = bot._build_status_blocks(
            body="", subtext="⏳ working. · 0s · 0s ago", keyboard=None, parse_mode="plain"
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "context")
        self.assertEqual(blocks[0]["elements"][0]["text"], "⏳ working. · 0s · 0s ago")
        self.assertEqual(fallback_text, "⏳ working. · 0s · 0s ago")

    def test_build_status_blocks_whitespace_body_is_context_only(self):
        bot = SlackBot.__new__(SlackBot)
        blocks, _ = bot._build_status_blocks(
            body="   ", subtext="⏳ working · 1s", keyboard=None, parse_mode="plain"
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "context")


class DiscordStatusContentTests(unittest.TestCase):
    def test_compose_status_content_uses_subtext_marker(self):
        content = DiscordBot._compose_status_content("🔧 x", "⏳ 5s")
        self.assertIn("-# ⏳ 5s", content)
        self.assertTrue(content.startswith("🔧 x"))

    def test_compose_status_content_empty_body_is_footer_only(self):
        # Footer-only bubble: just "-# {subtext}" with NO leading newlines.
        self.assertEqual(
            DiscordBot._compose_status_content("", "⏳ working. · 0s · 0s ago"),
            "-# ⏳ working. · 0s · 0s ago",
        )
        self.assertEqual(
            DiscordBot._compose_status_content(None, "⏳ working · 1s"),
            "-# ⏳ working · 1s",
        )

    def test_compose_status_content_without_subtext_is_unchanged(self):
        # No subtext → byte-identical body (every non-status caller).
        self.assertEqual(DiscordBot._compose_status_content("plain body", None), "plain body")

    def test_buttons_subtext_composes_footer_into_content(self):
        # send_message_with_buttons reuses _compose_status_content for the done
        # footer, so a result-with-buttons carries the ✅ done footer too.
        content = DiscordBot._compose_status_content("Final answer", "✅ done · 248k tok")
        self.assertTrue(content.startswith("Final answer"))
        self.assertTrue(content.endswith("-# ✅ done · 248k tok"))


class SlackContextFooterTests(unittest.TestCase):
    def test_build_context_footer_block_is_mrkdwn_context(self):
        bot = SlackBot.__new__(SlackBot)
        block = bot._build_context_footer_block("✅ done · 248k tok", parse_mode="plain")
        self.assertEqual(block["type"], "context")
        self.assertEqual(block["elements"][0]["text"], "✅ done · 248k tok")


class DeletionCapabilityTests(unittest.TestCase):
    def test_status_bubble_platforms_advertise_deletion(self):
        from config.platform_registry import get_platform_descriptor

        for platform in ("slack", "discord"):
            caps = get_platform_descriptor(platform).capabilities
            self.assertTrue(caps.supports_message_deletion, platform)
        # A non-deletion platform stays False (drives the collapse fallback).
        self.assertFalse(get_platform_descriptor("lark").capabilities.supports_message_deletion)

    def test_compose_status_content_truncates_body_to_fit_2000_limit(self):
        # Over-limit body is truncated so the footer survives within Discord's
        # 2000-char content cap, and the footer marker is preserved verbatim.
        subtext = "⏳ working"
        content = DiscordBot._compose_status_content("x" * 3000, subtext)
        self.assertIsNotNone(content)
        self.assertLessEqual(len(content), 2000)
        self.assertTrue(content.endswith(f"-# {subtext}"))


class SlackMarkdownOverLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_over_limit_markdown_keeps_legacy_path_without_subtext(self):
        # A result over the markdown-block cap (12k) but under the dispatcher's
        # inline limit must stay on the LEGACY mrkdwn path: passing subtext would
        # route it through the status-bubble send, which rejects bodies > the cap,
        # so the result would fail to post inline. The done-footer is dropped here.
        bot = SlackBot.__new__(SlackBot)
        bot._ensure_clients = lambda: None  # type: ignore[method-assign]
        calls: list[dict] = []

        async def _send_message(context, text, parse_mode=None, reply_to=None, subtext=None):
            calls.append({"len": len(text), "subtext": subtext})
            return "msg-legacy"

        bot.send_message = _send_message  # type: ignore[method-assign]
        ctx = MessageContext(user_id="U1", channel_id="C1", platform="slack")
        mid = await bot.send_markdown_message(ctx, "x" * 13000, subtext="✅ done · 248k tok")
        self.assertEqual(mid, "msg-legacy")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["subtext"])  # legacy path: no subtext attached


if __name__ == "__main__":
    unittest.main()
