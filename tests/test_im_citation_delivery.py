"""IM delivery of cited answers.

The Web transcript upgrades a citation into a badge from the structured sidecar.
Every IM platform instead gets exactly what it always got: ordinary Markdown
links in the message body, rendered to that platform's own clickable link
syntax by the formatter it already had. This file pins both halves of that -
the sidecar reaches persistence and never the wire, and the links survive each
platform's renderer with their source attribution intact.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.citations import (
    CitationSource,
    finalize_citations,
    materialize_citations,
    register_citations,
    resolve_citations,
)
from core.message_dispatcher import ConsolidatedMessageDispatcher
from modules.im import MessageContext
from modules.im.formatters.avibe_formatter import AvibeFormatter
from modules.im.formatters.discord_formatter import DiscordFormatter
from modules.im.formatters.feishu_formatter import FeishuFormatter
from modules.im.formatters.telegram_formatter import TelegramFormatter
from modules.im.formatters.wechat_formatter import WeChatFormatter
from modules.im.wechat import WeChatBot, WeChatConfig
from tests.test_message_dispatcher_platform_limits import _StubController

URL = "https://developers.openai.com/api/docs/guides/tools-web-search"
LINK = f"[developers.openai.com]({URL})"
ANSWER = "Native web search is documented."
CITED = f"{ANSWER} {LINK}"
MARKER = "\ue200cite\ue202turn0view0\ue201"
SOURCE = CitationSource(ref_id="turn0view0", title="Web search - OpenAI API", url=URL)


def cited(text: str = f"{ANSWER}{MARKER}"):
    """A registered answer and its bundle, exactly as the backend hands them on.

    What travels through delivery is an opaque token, not the link: every
    surface below is measured on what it makes of that, which is the only way
    a test can see a surface leak one or write the wrong body.
    """
    return register_citations(
        text, {SOURCE.ref_id: SOURCE}, unresolved_label="(source unavailable)"
    )


class CitationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def deliver(self, platform: str, text: str, **kwargs):
        controller = _StubController(platform)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="u1", channel_id="c1", platform=platform)
        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            await dispatcher.emit_agent_message(context, "result", text, **kwargs)
        return controller.im_client.sent, persist

    async def test_every_platform_receives_the_links_and_never_the_sidecar(self):
        registered, bundle = cited()
        for platform in ("slack", "telegram", "discord", "lark", "wechat"):
            with self.subTest(platform=platform):
                sent, _ = await self.deliver(platform, registered, citations=bundle)

                delivered = "".join(text for _, _, text, _ in sent)
                self.assertEqual(delivered, CITED)
                self.assertNotIn("ref_id", delivered)
                self.assertNotIn("turn0view0", delivered)

    async def test_the_sidecar_reaches_persistence(self):
        registered, bundle = cited()
        _, persist = await self.deliver("slack", registered, citations=bundle)

        persist.assert_called_once()
        # The body is persisted still holding its tokens: the mirror writes the
        # links and measures the spans after the last transform it will see.
        self.assertIs(persist.call_args.kwargs["citations"], bundle)
        body, citations = finalize_citations(persist.call_args.args[2], bundle)
        self.assertEqual(body, CITED)
        self.assertEqual([c["url"] for c in citations], [URL])

    async def test_an_uncited_answer_still_persists_without_a_sidecar(self):
        _, persist = await self.deliver("slack", "Plain answer.")

        self.assertIsNone(persist.call_args.kwargs["citations"])

    async def test_the_sidecar_does_not_change_how_a_long_answer_is_chunked(self):
        """Chunking reads the text only, so the two runs must be byte-identical."""
        controller = _StubController("wechat", max_bytes=1900)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="wechat-user", channel_id="wechat-user", platform="wechat")
        registered, bundle = cited(f"{'详' * 1200}\n\n{ANSWER}{MARKER}\n\n{'细' * 1200}")
        long_text = materialize_citations(registered, bundle)

        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await dispatcher.emit_agent_message(
                context, "result", registered, citations=bundle
            )
            with_sidecar = [text for _, _, text, _ in controller.im_client.sent]
            controller.im_client.sent.clear()
            await dispatcher.emit_agent_message(context, "result", long_text)
            without_sidecar = [text for _, _, text, _ in controller.im_client.sent]

        # A token is shorter than the link it stands for, so chunking it would
        # split the answer somewhere else: the links are written before the
        # body is measured, and the two runs come out identical.
        self.assertGreater(len(with_sidecar), 1)
        self.assertEqual(with_sidecar, without_sidecar)
        self.assertEqual("".join(with_sidecar), long_text)

    async def test_the_declared_parse_mode_is_untouched(self):
        registered, bundle = cited()
        sent, _ = await self.deliver(
            "slack", registered, parse_mode="markdown", citations=bundle
        )

        self.assertEqual([parse_mode for _, _, _, parse_mode in sent], ["markdown"])


class CitationRenderingPerPlatformTests(unittest.TestCase):
    """Each platform's own renderer, fed the exact text the backend emits."""

    def test_telegram_renders_an_anchor_carrying_the_source_domain(self):
        rendered = TelegramFormatter().render(CITED)

        self.assertIn(f'<a href="{URL}">developers.openai.com</a>', rendered)

    def test_telegram_keeps_a_cited_url_holding_markdown_punctuation_intact(self):
        """``safe_url`` percent-encodes the parens, so the link cannot end early."""
        url = "https://en.wikipedia.org/wiki/Foo_%28bar%29"

        rendered = TelegramFormatter().render(f"See [en.wikipedia.org]({url})")

        self.assertIn(f'<a href="{url}">en.wikipedia.org</a>', rendered)

    def test_telegram_leaves_a_cited_link_inside_a_code_block_literal(self):
        rendered = TelegramFormatter().render(f"```\n{LINK}\n```")

        self.assertIn("developers.openai.com](https", rendered)
        self.assertNotIn("<a href", rendered)

    def test_telegram_recognizes_a_link_whose_scheme_is_not_lowercase(self):
        """A scheme is case-insensitive. The link parser only knew the lowercase
        spelling, so an uppercase one was delivered as raw Markdown - which any
        link the agent writes in its own prose hits too, not only a citation."""
        rendered = TelegramFormatter().render("See [example.com](HTTPS://Example.com/X)")

        self.assertIn('<a href="HTTPS://Example.com/X">example.com</a>', rendered)

    def test_a_source_that_shouted_its_scheme_is_still_delivered_as_an_anchor(self):
        """The whole path, not just the formatter: an untrusted result spells
        the scheme in uppercase, and Telegram still shows a labeled hyperlink."""
        text, citations = resolve_citations(
            "Claimed.\ue200cite\ue202turn0view0\ue201",
            {
                "turn0view0": CitationSource(
                    ref_id="turn0view0", title="T", url="HTTPS://Example.com/X"
                )
            },
            unresolved_label="[source unavailable]",
        )

        rendered = TelegramFormatter().render(text)

        self.assertEqual(citations[0]["url"], "https://Example.com/X")
        self.assertIn('<a href="https://Example.com/X">example.com</a>', rendered)

    def test_markdown_native_platforms_pass_the_link_through(self):
        for formatter in (DiscordFormatter(), FeishuFormatter(), AvibeFormatter()):
            with self.subTest(formatter=type(formatter).__name__):
                self.assertEqual(
                    formatter.format_link("developers.openai.com", URL),
                    LINK,
                )

    def test_wechat_keeps_the_url_visible_because_it_has_no_hyperlinks(self):
        """Plain text, so attribution has to survive as readable text."""
        rendered = WeChatFormatter().format_link("developers.openai.com", URL)

        self.assertIn("developers.openai.com", rendered)
        self.assertIn(URL, rendered)


class WeChatAdapterRenderingTests(unittest.TestCase):
    """The real send path, not just the formatter it is supposed to agree with.

    ``WeChatBot.send_message`` puts the text through ``format_markdown`` before
    it reaches the API, so that method - and not ``WeChatFormatter`` on its own -
    decides what a WeChat user can read. A formatter test that never crosses this
    boundary cannot see the adapter drop the very address it renders.
    """

    def setUp(self):
        self.bot = WeChatBot(WeChatConfig(bot_token="test-token"))

    def test_the_adapter_keeps_the_destination_it_flattens(self):
        flattened = self.bot.format_markdown(CITED)

        self.assertIn(URL, flattened)
        self.assertEqual(flattened, f"Native web search is documented. developers.openai.com ({URL})")

    def test_the_adapter_writes_a_link_the_way_its_own_formatter_does(self):
        """One platform, one answer about how a link looks in plain text."""
        self.assertEqual(
            self.bot.format_markdown(LINK),
            WeChatFormatter().format_link("developers.openai.com", URL),
        )

    def test_flattening_a_link_never_lengthens_the_message(self):
        """``text (url)`` is one character shorter than ``[text](url)``.

        Chunking runs on the text the dispatcher holds, before this conversion,
        so keeping the address can only ever be safe if it cannot grow a chunk.
        """
        for text in (CITED, f"{LINK} {LINK}", f"**Bold** {LINK}", f"# Head\n{LINK}"):
            with self.subTest(text=text):
                self.assertLessEqual(len(self.bot.format_markdown(text)), len(text))

    def test_the_emphasis_passes_cannot_rewrite_a_destination(self):
        """A URL is full of characters the markdown strippers would eat."""
        url = "https://example.com/a_b_c/d*e*f/g`h"

        flattened = self.bot.format_markdown(f"See [example.com]({url}) now")

        self.assertEqual(flattened, f"See example.com ({url}) now")

    def test_a_self_titled_link_is_not_written_twice(self):
        url = "https://example.com/x"

        self.assertEqual(self.bot.format_markdown(f"[{url}]({url})"), url)

    def test_ordinary_markdown_is_still_reduced_to_plain_text(self):
        text = "# Title\n**bold** *italic* ~~gone~~ `code` and [a](https://example.com/a)"

        self.assertEqual(
            self.bot.format_markdown(text),
            "Title\nbold italic gone code and a (https://example.com/a)",
        )

    def test_an_image_is_still_reduced_to_its_alt_text(self):
        self.assertEqual(
            self.bot.format_markdown("![a screenshot](https://example.com/s.png)"),
            "a screenshot",
        )


class SlackCitationRenderingTests(unittest.TestCase):
    def test_slack_converts_the_link_to_mrkdwn(self):
        from markdown_to_mrkdwn import SlackMarkdownConverter

        rendered = SlackMarkdownConverter().convert(CITED)

        self.assertIn(f"<{URL}|developers.openai.com>", rendered)


if __name__ == "__main__":
    unittest.main()
