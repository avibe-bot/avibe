"""What a reader on each IM platform actually sees of a cited answer.

Every case here comes out of one real producer run recorded by
``tests/citation_bridge.py``: the text ``core.citations`` wrote, the sidecar it
wrote beside it, and the body ``core.reply_enhancer`` hands each surface. This
file then puts that body through each platform's own renderer - the real
``SlackBot`` conversion, the real ``TelegramBot``/``WeChatBot``
``format_markdown``, the real Discord and Feishu pass-throughs - and asks what
the reader ends up with.

The question is deliberately narrow, because attribution is a narrow promise: a
cited answer must show the source's name and reach the source's address. So the
assertions are on the visible label and the navigable destination, never on
"the URL appears somewhere". A citation whose host reads ``ab.example`` because
a converter ate the asterisks has kept the URL and lost the attribution, and
that is the bug this file exists to catch.

``ui/src/components/ui/citation-bridge.test.tsx`` reads the same fixture and
asks the same question of the real Web renderer. Discord and Feishu are measured
locally as pass-throughs only: nothing here sends a message, so nothing here can
claim how those platforms render one.
"""

from __future__ import annotations

import html
import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import (
    DiscordConfig,
    LarkConfig,
    SlackConfig,
    TelegramConfig,
    WeChatConfig,
)
from core.message_dispatcher import ConsolidatedMessageDispatcher
from modules.im import MessageContext
from modules.im.discord import DiscordBot
from modules.im.feishu import FeishuBot
from modules.im.slack import SlackBot
from modules.im.telegram import TelegramBot
from modules.im.wechat import WeChatBot
from tests.citation_bridge import FIXTURE, build
from tests.test_message_dispatcher_platform_limits import _StubController

BRIDGE: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = BRIDGE["cases"]


def case(key: str) -> dict[str, Any]:
    return next(row for row in CASES if row["key"] == key)


class BridgeFixtureTests(unittest.TestCase):
    def test_the_recorded_producer_output_is_what_the_producer_emits_today(self):
        """The fixture is a recording, so a stale one would test a past product.

        Regenerate with ``.venv/bin/python -m tests.citation_bridge``.
        """
        self.assertEqual(json.loads(json.dumps(build(), ensure_ascii=False)), BRIDGE)

    def test_every_case_carries_an_expectation_a_consumer_can_fail(self):
        for row in CASES:
            with self.subTest(case=row["key"]):
                self.assertTrue(row["why"].strip())
                if row["citations"]:
                    self.assertTrue(
                        any(anchor["cited"] for anchor in row["im_anchors"]),
                        "a cited case whose delivered text shows no citation proves nothing",
                    )

    def test_a_citation_claims_exactly_the_links_the_producer_wrote(self):
        """The counting contract, read back off the recording.

        Each sidecar row names one spelling and how many of its literal
        occurrences are its own; the anchors derived from the delivered text must
        add up to exactly that many, and every other anchor must stay ordinary.
        """
        for row in CASES:
            with self.subTest(case=row["key"]):
                claimed = sum(len(cite.get("occurrences") or ()) for cite in row["citations"])
                shown = [anchor for anchor in row["im_anchors"] if anchor["cited"]]
                # A claimed occurrence the reader is not shown is one inside a
                # code example: counted by both ends, rendered as a link by
                # neither. It may never be more than that.
                self.assertLessEqual(len(shown), claimed)
                for anchor in shown:
                    self.assertIn(
                        (anchor["url"], anchor["label"]),
                        [(cite["url"], cite["label"]) for cite in row["citations"]],
                    )


class PlatformConsumerTests(unittest.TestCase):
    """One platform's real renderer, asked what the reader sees.

    Subclasses supply ``render`` and the two readings a platform-specific
    assertion needs: ``links`` (what the reader can click, as
    ``(destination, label)``) and ``readable`` (the output with the platform's
    own escaping resolved, so a destination can be searched for as written).
    """

    __test__ = False

    def render(self, text: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def links(self, rendered: str) -> list[tuple[str, str]]:  # pragma: no cover
        raise NotImplementedError

    def readable(self, rendered: str) -> str:
        return rendered

    def test_a_citation_shows_its_source_and_reaches_its_address(self):
        """The whole promise: the reader sees the host and lands on the page."""
        for row in CASES:
            rendered = self.render(row["im_text"])
            clickable = self.links(rendered)
            for anchor in row["im_anchors"]:
                if not anchor["cited"]:
                    continue
                with self.subTest(case=row["key"], url=anchor["url"]):
                    self.assertIn((anchor["url"], anchor["label"]), clickable)

    def test_no_link_points_anywhere_the_producer_did_not_write(self):
        """A converter that reads a destination as text rewrites the address.

        Slack's mrkdwn converter did exactly that: ``https://a*b*.example/x``
        came back as ``https://a_b_.example/x``, a link to a different site that
        still looks like a citation.
        """
        for row in CASES:
            rendered = self.render(row["im_text"])
            written = {anchor["url"] for anchor in row["im_anchors"]}
            for destination, _ in self.links(rendered):
                with self.subTest(case=row["key"], destination=destination):
                    self.assertIn(destination, written)

    def test_every_address_the_answer_carried_survives_the_renderer(self):
        """Including the ones this platform does not turn into a link.

        A code example, a table cell or a footnote is rendered differently by
        every dialect here; what none of them may do is lose or alter the
        address the answer pointed at.
        """
        for row in CASES:
            readable = self.readable(self.render(row["im_text"]))
            for anchor in row["im_anchors"]:
                with self.subTest(case=row["key"], url=anchor["url"]):
                    self.assertIn(anchor["url"], readable)


class SlackConsumerTests(PlatformConsumerTests):
    __test__ = True

    # ``<destination|label>``, and the bare ``<destination>`` Slack also accepts.
    _LINK = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")

    def setUp(self):
        self.bot = SlackBot(SlackConfig(bot_token="xoxb-test"))

    def render(self, text: str) -> str:
        return self.bot._convert_markdown_to_slack_mrkdwn(text)

    def links(self, rendered: str) -> list[tuple[str, str]]:
        found = []
        for match in self._LINK.finditer(rendered):
            destination = match.group(1)
            if not destination.startswith(("http://", "https://")):
                continue  # ``<@U1>``/``<#C1>`` and stray angle brackets in prose
            found.append((destination, match.group(2) if match.group(2) is not None else destination))
        return found

    def test_the_converter_alone_would_have_sent_readers_to_another_site(self):
        """The counterexample, measured on both sides of the fix.

        ``markdown_to_mrkdwn`` scans the whole line for emphasis, destination
        included. Holding the destinations across its pass is what keeps the
        address the search engine returned.
        """
        from markdown_to_mrkdwn import SlackMarkdownConverter

        star = case("star_host")
        url = star["citations"][0]["url"]

        unheld = SlackMarkdownConverter().convert(star["im_text"])
        held = self.render(star["im_text"])

        self.assertIn("https://a_b_.example/x", unheld)
        self.assertNotIn(url, unheld)
        self.assertIn(f"<{url}|a*b*.example>", held)

    def test_a_destination_is_held_through_the_query_string_too(self):
        query = case("backtick_query")
        url = query["citations"][0]["url"]

        self.assertIn(f"<{url}|example.com>", self.render(query["im_text"]))


class TelegramConsumerTests(PlatformConsumerTests):
    __test__ = True

    _ANCHOR = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.DOTALL)

    def setUp(self):
        self.bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))

    def render(self, text: str) -> str:
        return self.bot.format_markdown(text)

    def readable(self, rendered: str) -> str:
        return html.unescape(rendered)

    def links(self, rendered: str) -> list[tuple[str, str]]:
        return [
            (html.unescape(match.group(1)), html.unescape(match.group(2)))
            for match in self._ANCHOR.finditer(rendered)
        ]

    def test_the_label_is_not_re_read_as_emphasis(self):
        """A host holding ``*`` was shown as ``ab.example`` with an italic ``b``."""
        star = case("star_host")

        rendered = self.render(star["im_text"])

        self.assertIn('<a href="https://a*b*.example/x">a*b*.example</a>', rendered)
        self.assertNotIn("<i>", rendered)


class WeChatConsumerTests(PlatformConsumerTests):
    __test__ = True

    def setUp(self):
        self.bot = WeChatBot(WeChatConfig(bot_token="test-token"))

    def render(self, text: str) -> str:
        return self.bot.format_markdown(text)

    def links(self, rendered: str) -> list[tuple[str, str]]:
        """WeChat has no hyperlinks, so ``label (address)`` is the link.

        Reading it back needs the labels the answer actually used - a general
        scan of "text before parentheses" would call the whole sentence a label.
        """
        found = []
        for row in CASES:
            for anchor in row["im_anchors"]:
                flattened = f"{anchor['label']} ({anchor['url']})"
                for _ in range(rendered.count(flattened)):
                    found.append((anchor["url"], anchor["label"]))
        return found

    def test_the_address_stays_readable_because_nothing_is_clickable(self):
        star = case("star_host")

        self.assertEqual(
            self.render(star["im_text"]),
            "Cited. a*b*.example (https://a*b*.example/x)",
        )


class PassThroughConsumerTests(unittest.TestCase):
    """Discord and Feishu render Markdown themselves and are handed it as-is.

    That is all this measures: the adapter changes nothing. What those two
    platforms then draw is not observable from here, and no message is sent.
    """

    def setUp(self):
        self.discord = DiscordBot(DiscordConfig(bot_token="test-token"))
        self.feishu = FeishuBot(LarkConfig(app_id="app-id", app_secret="app-secret"))

    def test_the_adapter_hands_the_body_over_unchanged(self):
        for row in CASES:
            with self.subTest(case=row["key"]):
                self.assertEqual(self.discord.format_markdown(row["im_text"]), row["im_text"])
                self.assertEqual(self.feishu.format_markdown(row["im_text"]), row["im_text"])


class TranscriptPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """The workbench row the Web renderer later reads back.

    IM and the transcript are handed different bodies on purpose - a ``file://``
    link is flattened for IM and kept for the workbench media proxy - so the
    recording carries both, and this pins that the dispatcher really produces
    them from one agent reply.
    """

    async def persist(self, row: Mapping[str, Any], platform: str):
        controller = _StubController(platform)
        controller.config.reply_enhancements = True
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="u1", channel_id="c1", platform=platform)
        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            await dispatcher.emit_agent_message(
                context,
                "result",
                row["delivered"],
                citations=row["citations"] or None,
            )
        return controller.im_client.sent, persist

    async def test_the_transcript_keeps_the_body_the_web_renderer_needs(self):
        for key in ("file_link", "quick_replies", "star_host", "silent_strip"):
            row = case(key)
            with self.subTest(case=key):
                _, persist = await self.persist(row, "avibe")

                persist.assert_called_once()
                self.assertEqual(persist.call_args.args[2], row["web_text"])
                self.assertEqual(
                    persist.call_args.kwargs["quick_replies"],
                    row["quick_replies"] or None,
                )
                self.assertEqual(
                    persist.call_args.kwargs["citations"],
                    row["citations"] or None,
                )

    async def test_im_receives_the_flattened_body_and_never_the_sidecar(self):
        for key in ("file_link", "quick_replies", "star_host", "silent_strip"):
            row = case(key)
            with self.subTest(case=key):
                sent, _ = await self.persist(row, "slack")

                delivered = "".join(text for _, _, text, _ in sent)
                self.assertEqual(delivered, row["im_text"])
                for cite in row["citations"]:
                    self.assertNotIn(cite["ref_id"], delivered)
                    self.assertNotIn("spelling", delivered)


if __name__ == "__main__":
    unittest.main()
