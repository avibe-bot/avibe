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
import os
import re
import sys
import tempfile
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
from core.citations import (
    CitationSource,
    body_digest,
    citation_ref_ids,
    register_citations,
    resolve_citations,
)
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.reply_enhancer import inline_links, unescape_markdown
from modules.im import MessageContext
from modules.im.discord import DiscordBot
from modules.im.feishu import FeishuBot
from modules.im.slack import SlackBot
from modules.im.telegram import TelegramBot
from modules.im.wechat import WeChatBot
from storage import messages_service
from storage.db import create_sqlite_engine, dispose_cached_sqlite_engines
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions
from storage.settings_service import upsert_scope
from tests.citation_bridge import FIXTURE, UNRESOLVED_LABEL, build, utf16_slice
from tests.test_message_dispatcher_platform_limits import _StubController

BRIDGE: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = BRIDGE["cases"]
ROWS: list[dict[str, Any]] = BRIDGE["rows"]


def case(key: str) -> dict[str, Any]:
    return next(row for row in CASES if row["key"] == key)


def stored_row(key: str) -> dict[str, Any]:
    return next(row["row"] for row in ROWS if row["key"] == key)


def sources_of(row: Mapping[str, Any]) -> dict[str, CitationSource]:
    """The search results the backend captured, as the producer receives them."""
    return {
        ref: CitationSource(ref_id=ref, url=meta["url"], title=meta["title"])
        for ref, meta in row["sources"].items()
    }


def registered(row: Mapping[str, Any]):
    """Re-run the producer for one recorded case.

    The recording cannot carry the bundle itself: the tokens in it are nonces,
    minted per run and never written to disk. So a test that needs the
    dispatcher's real input registers the same agent text against the same
    sources again, and checks the bodies that come out against the recording.
    """
    return register_citations(
        row["agent_text"], sources_of(row), unresolved_label=UNRESOLVED_LABEL
    )


MEDIA_TOKEN_RE = re.compile(r"/api/media/[A-Za-z0-9_-]+")


def without_media_ids(payload: Any) -> Any:
    """The one thing in a recorded row that is minted fresh on every run.

    Persisting a ``file://`` attachment registers it and writes a proxy URL
    carrying a new random id, so the stored body - and therefore its digest -
    differs between two identical runs. Both are blanked here so the rest of
    the row is still compared byte for byte; that the digest describes the body
    it was written with is asserted on the recorded row itself, below.
    """
    if isinstance(payload, dict):
        blanked = {key: without_media_ids(value) for key, value in payload.items()}
        if isinstance(blanked.get("text"), str) and MEDIA_TOKEN_RE.search(blanked["text"]):
            blanked["text"] = MEDIA_TOKEN_RE.sub("/api/media/<id>", blanked["text"])
            for cite in blanked.get("citations") or ():
                cite["body_sha256"] = "<digest of a body holding a fresh media id>"
        return blanked
    if isinstance(payload, list):
        return [without_media_ids(item) for item in payload]
    return payload


class BridgeFixtureTests(unittest.TestCase):
    def test_the_recorded_producer_output_is_what_the_producer_emits_today(self):
        """The fixture is a recording, so a stale one would test a past product.

        Regenerate with ``.venv/bin/python -m tests.citation_bridge``.
        """
        self.assertEqual(
            without_media_ids(json.loads(json.dumps(build(), ensure_ascii=False))),
            without_media_ids(BRIDGE),
        )

    def test_every_recorded_row_is_a_row_a_reader_could_lose_a_badge_on(self):
        """The rows are the Web consumers' input, so they have to be real ones.

        A row whose sidecar does not describe its own stored text would make
        the Web test assert a refusal; a set where no row carries a folded
        footer would never exercise the split that makes the measurement have
        to travel. Both would pass quietly.
        """
        folded = 0
        for row in ROWS:
            with self.subTest(case=row["key"]):
                stored = row["row"]
                self.assertTrue(row["why"].strip())
                content = stored["content"]
                [cite] = content["citations"]
                self.assertEqual(cite["body_sha256"], body_digest(stored["text"]))
                self.assertEqual(
                    utf16_slice(stored["text"], *cite["spans"][0]),
                    f"[{cite['label']}]({cite['url']})",
                )
                footer = content.get("result_footer")
                if footer and stored["text"].rstrip().endswith(footer):
                    folded += 1
                    # The strip removes a range AFTER the citation here. That
                    # is what makes the row survivable; a fixture where the
                    # footer preceded it would be a different question.
                    self.assertLess(cite["spans"][0][1], len(stored["text"]))
        self.assertTrue(folded, "no recorded row folds a footer into its stored body")

    def test_the_rows_include_one_a_reader_edits_twice_more_after_the_backend(self):
        """The combination row, checked to still be one.

        Its value is entirely in what has already happened to it: the same
        reply persisted down both paths, where the attachment is rewritten
        before the sidecar is measured - to a proxy link on the workbench, to a
        bare label on IM - and a ``$<NAME>`` marker sits above the citation for
        the reader to turn into a card. A bridge edit that dropped any of those
        would leave the Web test passing on a plainer row than it names.
        """
        rewritten = stored_row("workbench_row_rewritten_and_carded")
        self.assertRegex(rewritten["text"], r"^\[report\]\(/api/media/[^)]+\)")
        # The workbench stores its footer apart, so the reader's only edit here
        # is the card; the IM row below gets the strip as well.
        self.assertNotIn(rewritten["content"]["result_footer"], rewritten["text"])

        flattened = stored_row("im_row_flattened_carded_and_folded")
        self.assertTrue(flattened["text"].startswith("report\n\n"))
        self.assertNotIn("](", flattened["text"].split("\n\n")[0])
        self.assertTrue(
            flattened["text"].rstrip().endswith(flattened["content"]["result_footer"])
        )

        for row in (rewritten, flattened):
            [cite] = row["content"]["citations"]
            # Every one of those sits above the citation, which is the only
            # place an edit can move it.
            before = utf16_slice(row["text"], 0, cite["spans"][0][0])
            self.assertIn("report", before)
            self.assertIn("$<deployKey>", before)
            self.assertEqual(cite["body_sha256"], body_digest(row["text"]))

    def test_every_case_carries_an_expectation_a_consumer_can_fail(self):
        for row in CASES:
            with self.subTest(case=row["key"]):
                self.assertTrue(row["why"].strip())
                if row["im_citations"]:
                    self.assertTrue(
                        any(anchor["cited"] for anchor in row["im_anchors"]),
                        "a cited case whose delivered text shows no citation proves nothing",
                    )

    def test_a_citation_claims_exactly_the_links_the_producer_wrote(self):
        """The binding contract, read back off the recording.

        Each sidecar row names the spans its links occupy in one exact body;
        the anchors derived from that body must add up to at most that many,
        and every other anchor must stay ordinary.
        """
        for row in CASES:
            with self.subTest(case=row["key"]):
                claimed = sum(len(cite["spans"]) for cite in row["im_citations"])
                shown = [anchor for anchor in row["im_anchors"] if anchor["cited"]]
                # A claimed occurrence the reader is not shown is one inside a
                # code example: counted by both ends, rendered as a link by
                # neither. It may never be more than that.
                self.assertLessEqual(len(shown), claimed)
                for anchor in shown:
                    self.assertIn(
                        (anchor["url"], anchor["label"]),
                        [(cite["url"], cite["label"]) for cite in row["im_citations"]],
                    )

    def test_two_deletions_that_cancel_out_do_not_make_a_second_citation(self):
        """The counterexample that decides where a citation gets its identity.

        ``file_delete_splice`` is one reply that cited one page once. Delivery
        removes two ranges from it - an attachment link collapses to its label,
        a ``<silent>`` block leaves - and the characters left behind close up
        into a second complete marker the model never wrote. On the workbench
        the attachment survives as a link, so the same reply splices on one
        surface and not on the other.

        Resolving the delivered body, which is all a stage placed after
        delivery can do, is run here against the real recording: it writes a
        second link to the source and puts the badge on it, leaving the
        sentence the model actually cited an ordinary anchor. Registration
        happens before either deletion instead, so the shipped sidecar claims
        the one occurrence that was cited and the spliced shape stays literal.
        """
        row = case("file_delete_splice")
        spliced = "\ue200cite\ue202turn0view0\ue201"
        # The model wrote one marker, in the first sentence. The second
        # sentence held halves that were not one: an attachment link sat
        # between ``ci`` and ``te``, a ``<silent>`` block before the
        # terminator.
        self.assertEqual(row["agent_text"].count(spliced), 1)
        self.assertTrue(row["agent_text"].startswith(f"Actual. {spliced}"))
        # After delivery the marker the model wrote is a link, and the only
        # one still standing is the one it did not write.
        self.assertEqual(row["im_text"].count(spliced), 1)
        self.assertTrue(row["im_text"].endswith(f"Synthetic. {spliced}"))
        self.assertNotIn(spliced, row["web_text"])
        self.assertEqual(citation_ref_ids(row["im_text"]), ["turn0view0"])
        self.assertEqual(citation_ref_ids(row["web_text"]), [])

        [shipped] = row["im_citations"]
        self.assertEqual(len(shipped["spans"]), 1)
        cited = utf16_slice(row["im_text"], *shipped["spans"][0])
        self.assertEqual(cited, f"[{shipped['label']}]({shipped['url']})")
        # The one link the sidecar claims is the sentence that carried the
        # marker in the model's own text, not the one the deletions formed.
        self.assertTrue(row["im_text"].startswith(f"Actual. {cited}"))

        late_body, late = resolve_citations(
            row["im_text"], sources_of(row), unresolved_label=UNRESOLVED_LABEL
        )
        [late_cite] = late
        self.assertEqual(late_body.count(cited), 2)
        self.assertEqual(len(late_cite["spans"]), 1)
        self.assertNotEqual(late_cite["spans"], shipped["spans"])
        self.assertTrue(late_body.endswith(f"Synthetic. {cited}"))


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
    # The three references Slack resolves on the way in, and the only ones: a
    # label spelling ``&copy;`` is shown as those six characters, not ``(c)``.
    # A label arrives spelled this way because ``&``, ``<`` and ``>`` are
    # Slack's own markup, so reading back what the reader sees means resolving
    # these three and nothing else.
    _RESOLVED = {"&amp;": "&", "&lt;": "<", "&gt;": ">"}
    _REFERENCE = re.compile(r"&(?:amp|lt|gt);")

    def setUp(self):
        self.bot = SlackBot(SlackConfig(bot_token="xoxb-test"))

    def render(self, text: str) -> str:
        return self.bot._convert_markdown_to_slack_mrkdwn(text)

    def _resolve(self, text: str) -> str:
        return self._REFERENCE.sub(lambda match: self._RESOLVED[match.group()], text)

    def links(self, rendered: str) -> list[tuple[str, str]]:
        found = []
        for match in self._LINK.finditer(rendered):
            destination = match.group(1)
            if not destination.startswith(("http://", "https://")):
                continue  # ``<@U1>``/``<#C1>`` and stray angle brackets in prose
            # The label is resolved, the destination is not: an address is
            # delivered as it stands, so an encoded one is a defect this
            # reading must keep visible rather than undo.
            label = match.group(2)
            found.append((destination, self._resolve(label) if label is not None else destination))
        return found

    def test_the_converter_alone_would_have_sent_readers_to_another_site(self):
        """The counterexample, measured on both sides of the fix.

        ``markdown_to_mrkdwn`` scans the whole line for emphasis, destination
        included. Holding the destinations across its pass is what keeps the
        address the search engine returned.
        """
        from markdown_to_mrkdwn import SlackMarkdownConverter

        star = case("star_host")
        url = star["im_citations"][0]["url"]

        unheld = SlackMarkdownConverter().convert(star["im_text"])
        held = self.render(star["im_text"])

        self.assertIn("https://a_b_.example/x", unheld)
        self.assertNotIn(url, unheld)
        self.assertIn(f"<{url}|a*b*.example>", held)

    def test_a_destination_is_held_through_the_query_string_too(self):
        query = case("backtick_query")
        url = query["im_citations"][0]["url"]

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
    """The workbench row the Web renderer reads back, read back out of SQLite.

    Everything above measures the producer in memory. This drives the real
    ``ConsolidatedMessageDispatcher`` with real persistence into a temporary
    SQLite home, then reads the rows back through ``list_session_messages`` -
    the same query the transcript endpoint runs - and checks them against the
    recording. That equality is what makes the Web half of the bridge a test of
    the stored row: ``citation-bridge.test.tsx`` renders ``web_text`` and
    ``web_citations``, and these are the bytes the database returned.

    IM and the transcript are handed different bodies on purpose - a ``file://``
    link is flattened for IM and kept for the workbench media proxy - so the
    recording carries both.
    """

    SESSION = "ses_bridge"
    NOW = "2026-09-21T12:00:00Z"

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"AVIBE_HOME": self._home.name})
        self._env.start()
        dispose_cached_sqlite_engines()
        ensure_sqlite_state()
        engine = create_sqlite_engine()
        try:
            with engine.begin() as conn:
                scope_id = upsert_scope(
                    conn,
                    platform="avibe",
                    scope_type="project",
                    native_id="proj_bridge",
                    now=self.NOW,
                )
                conn.execute(
                    agent_sessions.insert().values(
                        id=self.SESSION,
                        scope_id=scope_id,
                        agent_backend="codex",
                        agent_variant="default",
                        session_anchor=f"anchor_{self.SESSION}",
                        native_session_id="",
                        status="active",
                        metadata_json="{}",
                        created_at=self.NOW,
                        updated_at=self.NOW,
                        last_active_at=self.NOW,
                    )
                )
        finally:
            engine.dispose()

    def tearDown(self):
        dispose_cached_sqlite_engines()
        self._env.stop()
        self._home.cleanup()

    async def deliver(self, row: Mapping[str, Any], platform: str):
        controller = _StubController(platform)
        controller.config.reply_enhancements = True
        dispatcher = ConsolidatedMessageDispatcher(controller)
        avibe = platform == "avibe"
        context = MessageContext(
            user_id="workbench" if avibe else "u1",
            channel_id=self.SESSION if avibe else "c1",
            platform=platform,
            platform_specific={"agent_session_id": self.SESSION} if avibe else None,
        )
        text, bundle = registered(row)
        await dispatcher.emit_agent_message(context, "result", text, citations=bundle)
        return controller.im_client.sent

    def stored(self) -> list[tuple[str, Any]]:
        engine = create_sqlite_engine()
        try:
            with engine.connect() as conn:
                transcript = messages_service.list_session_messages(
                    conn, session_id=self.SESSION
                )
        finally:
            engine.dispose()
        return [
            (message["text"], (message["content"] or {}).get("citations"))
            for message in transcript["messages"]
        ]

    def assertDescribesItsOwnBody(self, text: str, citations):
        """Each row measures THIS body: its digest, and its own link at its span.

        What a span covers is read with the delivery pass's own link scanner
        rather than compared to a rebuilt spelling, because the label in the
        body is Markdown-escaped and the one in the sidecar is the host. The
        property is that the span is exactly one whole link, to this source.
        """
        for cite in citations or ():
            self.assertEqual(cite["body_sha256"], body_digest(text))
            for span in cite["spans"]:
                spelled = utf16_slice(text, *span)
                found = list(inline_links(spelled))
                self.assertEqual(len(found), 1)
                self.assertEqual((found[0].start, found[0].end), (0, len(spelled)))
                self.assertEqual(found[0].destination, cite["url"])
                self.assertEqual(
                    unescape_markdown(spelled[found[0].label_start : found[0].label_end]),
                    cite["label"],
                )

    async def test_the_row_the_web_renderer_is_handed_is_the_one_sqlite_returns(self):
        for row in CASES:
            await self.deliver(row, "avibe")
        stored = self.stored()
        self.assertEqual(len(stored), len(CASES))

        for row, (text, citations) in zip(CASES, stored):
            with self.subTest(case=row["key"]):
                self.assertDescribesItsOwnBody(text, citations)
                if "file://" in row["web_text"]:
                    # Rewritten on its way to disk; measured in its own test
                    # below, where what changed is the point.
                    continue
                self.assertEqual(text, row["web_text"])
                self.assertEqual(citations, row["web_citations"] or None)

    async def test_the_media_rewrite_moves_the_measurement_it_walks_past(self):
        """The one transform that runs after delivery and before the sidecar.

        A ``file://`` attachment is registered and swapped for a media-proxy URL
        inside the same transaction that writes the row, so the stored body is
        not the one delivery produced - it is shorter, and a citation below the
        attachment sits somewhere else in it. The sidecar is measured after that
        rewrite for exactly this reason: a span carried over from the delivered
        text would point into the wrong characters, and the digest would say so.
        """
        for key in ("file_link", "file_link_above"):
            await self.deliver(case(key), "avibe")
        stored = self.stored()

        for key, (text, citations) in zip(("file_link", "file_link_above"), stored):
            row = case(key)
            with self.subTest(case=key):
                self.assertNotIn("file://", text)
                self.assertRegex(text, r"\[report\]\(/api/media/[^)]+\)")
                self.assertDescribesItsOwnBody(text, citations)
                # Same citation, same link, different body: the recorded
                # delivery measurement does not describe what was stored.
                recorded = row["web_citations"][0]
                self.assertEqual(citations[0]["url"], recorded["url"])
                self.assertNotEqual(citations[0]["body_sha256"], recorded["body_sha256"])

        # Above the citation the rewrite shortens the text, so the span moves;
        # below it nothing before the link changed, so it does not.
        self.assertEqual(stored[0][1][0]["spans"], case("file_link")["web_citations"][0]["spans"])
        self.assertNotEqual(
            stored[1][1][0]["spans"], case("file_link_above")["web_citations"][0]["spans"]
        )

    async def test_im_receives_the_flattened_body_and_never_the_sidecar(self):
        for row in CASES:
            with self.subTest(case=row["key"]):
                sent = await self.deliver(row, "slack")

                delivered = "".join(text for _, _, text, _ in sent)
                self.assertEqual(delivered, row["im_text"])
                for cite in row["im_citations"]:
                    # The sidecar's own fields: a reader on IM is shown links,
                    # and the measurement that binds them to a body stays on
                    # the row. A ``ref_id`` is NOT asserted absent - a marker
                    # the producer refused to register keeps its literal text,
                    # spelled ref and all (``splice``), which is the point of
                    # refusing it.
                    self.assertNotIn(cite["body_sha256"], delivered)
                    self.assertNotIn("spans", delivered)

    async def test_the_extracted_buttons_and_labels_carry_their_own_copy(self):
        """Quick-reply labels and file labels leave the body and stay text."""
        row = case("quick_replies")
        await self.deliver(row, "avibe")

        engine = create_sqlite_engine()
        try:
            with engine.connect() as conn:
                transcript = messages_service.list_session_messages(
                    conn, session_id=self.SESSION
                )
        finally:
            engine.dispose()
        content = transcript["messages"][0]["content"] or {}
        self.assertEqual(content.get("quick_replies"), row["quick_replies"])


if __name__ == "__main__":
    unittest.main()
