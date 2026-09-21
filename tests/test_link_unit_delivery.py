"""Where a link is, and what a platform owes the whole of it.

A Markdown link is one unit: a label a reader taps and an address the tap goes
to. Every IM dialect spells that unit its own way, and the passes that do it
are line scanners - Slack's ``markdown_to_mrkdwn`` reads a whole line for
emphasis and for brackets alike. Two things went wrong there. It read
``https://a*b*.example/x`` as emphasis and delivered a link to
``https://a_b_.example/x``, a different site; and once the address was held
behind a placeholder, the brackets still in the stream were its to mis-pair -
``[^f]: [p](https://example.com/x)`` went out as one link labelled ``^f]: [p``.

So the unit is located once, in the shared layer, and each platform is asked to
spell it again from the label and the destination a Markdown reader resolves.
Both halves are measured here: which source ranges a link can be found in -
including the ones CommonMark hands to no inline parser, because the consumer
downstream reads them anyway - and what the real Slack client sends for each.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import WeChatConfig
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.reply_enhancer import inline_links
from modules.im import MessageContext
from modules.im.formatters import hold_links, restore_held
from modules.im.slack import SlackBot, SlackMarkdownConverter
from modules.im.wechat import WeChatBot, wechat_api, wechat_cdn
from tests.test_message_dispatcher_result_fallback import _StubController

STAR = "https://a*b*.example/x"
PLAIN = "https://example.com/x"


def located(text: str) -> list[tuple[str, str, str]]:
    """Every link in *text* as ``(whole unit, label, resolved destination)``."""
    return [
        (
            text[link.start : link.end],
            text[link.label_start : link.label_end],
            link.destination,
        )
        for link in inline_links(text)
    ]


def slack(text: str) -> str:
    # The conversion is the whole unit under test; the rest of the client is
    # network plumbing this never reaches.
    bot = SlackBot.__new__(SlackBot)
    bot.markdown_converter = SlackMarkdownConverter()
    return bot._convert_markdown_to_slack_mrkdwn(text)


def converter_alone(text: str) -> str:
    """What the third-party converter does with no protection at all.

    Used only where the contract is "this text is NOT ours to change" - code and
    images, compared against the converter of whatever version is installed. The
    mangling that motivated this change is documented in prose rather than
    asserted: pinning one vendor version's particular wrong output would make an
    upstream fix read as a regression here, and the delivered link is the thing
    this module owes the reader either way.
    """
    return SlackMarkdownConverter().convert(text)


class TestWhereALinkIs:
    """The source ranges a link has to be findable in.

    A block CommonMark parses inline is the easy half. The other half is the
    lines it parses inline for nobody - a link reference definition is consumed
    whole by the block parser and an HTML block is passed through raw - which
    the Web renderer treats its own way but the IM converters read as ordinary
    lines of text.
    """

    def test_a_link_in_prose_is_located_whole(self):
        text = f"Read [the docs]({STAR}) first."

        assert located(text) == [(f"[the docs]({STAR})", "the docs", STAR)]

    def test_a_link_in_a_footnote_definition_is_found(self):
        """The line CommonMark hands to no inline parser at all.

        Nothing found it before, so nothing protected it, and Slack's converter
        paired the definition's own bracket with the link's - the reader got
        one link labelled ``^f]: [p`` pointing at a rewritten address.
        """
        text = f"[^f]: [p]({STAR})\n\nCited. [q]({PLAIN}) [^f]"

        assert located(text) == [
            (f"[p]({STAR})", "p", STAR),
            (f"[q]({PLAIN})", "q", PLAIN),
        ]

    def test_a_link_in_an_html_block_is_found(self):
        text = f"<div>\n[x]({STAR})\n</div>"

        assert located(text) == [(f"[x]({STAR})", "x", STAR)]

    def test_a_link_in_a_table_cell_is_found(self):
        text = f"| Source |\n| --- |\n| [t]({STAR}) |"

        assert located(text) == [(f"[t]({STAR})", "t", STAR)]

    def test_a_link_written_across_two_lines_is_one_unit(self):
        text = f"See [the\nguide]({STAR}) here."

        assert located(text) == [(f"[the\nguide]({STAR})", "the\nguide", STAR)]

    def test_a_link_inside_a_code_span_that_spans_two_lines_is_not_a_link(self):
        """The counterexample to reading each line on its own.

        A backtick pair is a code span across the whole paragraph, and the link
        it holds is an example rather than an address. A pass that scanned
        every line separately - or masked backticks one line at a time - would
        find one here, and would then hand a platform a link no reader is
        shown. Lines a block already claimed are read as that block.
        """
        text = f"Text `code\n[x]({STAR})` more"

        assert located(text) == []

    def test_a_link_inside_code_on_one_line_is_not_a_link(self):
        for text in (
            f"An example: `[x]({STAR})`",
            f"```\n[x]({STAR})\n```",
            f"    [x]({STAR})\n",
        ):
            assert located(text) == []

    def test_an_image_is_not_a_link(self):
        assert located(f"![alt]({STAR})") == []

    def test_a_reference_link_spells_no_destination(self):
        assert located(f"[a][ref]\n\n[ref]: {STAR}") == []

    def test_neighbouring_links_are_each_located_once(self):
        """Two units touching, so a caller may splice on these offsets in one pass."""
        text = f"[a]({PLAIN})[b]({STAR})"
        links = inline_links(text)

        assert located(text) == [(f"[a]({PLAIN})", "a", PLAIN), (f"[b]({STAR})", "b", STAR)]
        assert links[0].end == links[1].start

    def test_a_title_is_not_part_of_the_address(self):
        text = f'[docs]({PLAIN} "T")'

        assert located(text) == [(text, "docs", PLAIN)]


class TestHoldingTheUnit:
    def test_each_unit_is_replaced_once_and_restored_verbatim(self):
        text = f"See [one]({PLAIN}) and [two]({STAR})."

        held, tokens = hold_links(text, render=lambda label, url: f"<{url}|{label}>")

        assert "](" not in held
        assert len(tokens) == 2
        assert restore_held(held, tokens) == (
            f"See <{PLAIN}|one> and <{STAR}|two>."
        )

    def test_the_platform_is_handed_the_label_and_the_resolved_destination(self):
        """Resolved: angle brackets and a title are spelling, not address."""
        seen: list[tuple[str, str]] = []

        hold_links(
            f'[a](<{STAR}>) and [b]({PLAIN} "T")',
            render=lambda label, url: seen.append((label, url)) or "",
        )

        assert seen == [("a", STAR), ("b", PLAIN)]

    def test_a_text_with_no_link_is_handed_back_untouched(self):
        text = "Just *em* and **bold** and `code`."

        assert hold_links(text, render=lambda label, url: "") == (text, {})


class TestSlackSendsTheWholeUnit:
    """The real client, on each range the converter reads as a line of text."""

    def test_a_footnote_definition_reaches_slack_as_the_link_it_spells(self):
        text = f"[^f]: [p]({STAR})"

        assert slack(text) == f"[^f]: <{STAR}|p>"

    def test_an_html_block_keeps_the_address_it_holds(self):
        text = f"<div>\n[x]({STAR})\n</div>"

        assert slack(text) == f"<div>\n<{STAR}|x>\n</div>"

    def test_a_table_cell_becomes_a_link_instead_of_raw_markdown(self):
        text = f"| Source |\n| --- |\n| [t]({STAR}) |"

        assert slack(text) == f"*Source*\n<{STAR}|t>"

    def test_a_label_written_across_two_lines_arrives_as_one_link(self):
        """A newline inside ``<url|label>`` is not a link, and a soft break is a space.

        The converter's link pattern never crosses a line, so on its own it
        left the Markdown standing - with the address already rewritten by the
        emphasis pass that does not stop at one either.
        """
        text = f"See [the\nguide]({STAR}) here."

        assert slack(text) == f"See <{STAR}|the guide> here."

    def test_a_titled_link_no_longer_sends_the_title_as_part_of_the_address(self):
        text = f'[docs]({PLAIN} "T")'

        assert slack(text) == f"<{PLAIN}|docs>"

    def test_a_formatted_label_is_still_mrkdwn(self):
        """Holding the unit takes the label out of the converter's reach, so the
        platform that spells the unit formats the label itself."""
        assert slack(f"[**bold**]({PLAIN})") == f"<{PLAIN}|*bold*>"

    def test_a_link_with_no_label_is_sent_bare(self):
        assert slack(f"[]({PLAIN})") == f"<{PLAIN}>"

    def test_prose_around_a_link_is_formatted_as_before(self):
        assert slack(f"See [one]({PLAIN}) and *em* text.") == (
            f"See <{PLAIN}|one> and _em_ text."
        )

    def test_a_code_example_keeps_the_behaviour_it_had(self):
        """Code is not a link on any surface, and holding stops at its edge."""
        for text in (f"```\n[x]({STAR})\n```", f"Text `code\n[x]({STAR})` more"):
            assert slack(text) == converter_alone(text)

    def test_an_image_keeps_the_behaviour_it_had(self):
        text = f"![alt]({STAR})"

        assert slack(text) == converter_alone(text)


class TestSlackSpellsTheWholeLabel:
    """What a label owes Slack once the unit is Slack's to serialize.

    ``<url|label>`` ends at the first ``>``, so a label that means one
    literally takes the address down with it: the reader is shown the rest of
    the label and the raw URL, with nothing to click. Slack reads ``&`` and
    ``<`` as markup of its own for the same reason, and a label that came from
    a backslash escape only becomes those characters at restore time - after
    the wrapper used to be built.
    """

    def test_a_label_that_means_a_delimiter_arrives_as_one(self):
        assert slack(f"[a > b]({PLAIN})") == f"<{PLAIN}|a &gt; b>"
        assert slack(f"[Tom & Jerry]({PLAIN})") == f"<{PLAIN}|Tom &amp; Jerry>"
        assert slack(f"[5 < 6]({PLAIN})") == f"<{PLAIN}|5 &lt; 6>"

    def test_a_backslash_escaped_delimiter_is_encoded_not_restored_raw(self):
        """The ordering case: these characters come back after the converter ran.

        An escape says the character is not syntax, so it is held through the
        platform pass and restored afterwards. Restored into a finished
        ``<url|label>`` it would be a raw delimiter inside the wrapper with
        nothing left to encode it - the link would end at the ``>``.
        """
        assert slack(rf"[a \> b \& c \< d]({PLAIN})") == f"<{PLAIN}|a &gt; b &amp; c &lt; d>"

    def test_a_reference_the_source_already_spells_is_not_encoded_twice(self):
        """``&amp;`` means ``&`` to the reader, and Slack resolves it too."""
        assert slack(f"[AT&amp;T]({PLAIN})") == f"<{PLAIN}|AT&amp;T>"
        assert slack(f"[a &lt;b&gt; c]({PLAIN})") == f"<{PLAIN}|a &lt;b&gt; c>"

    def test_a_reference_slack_does_not_resolve_is_spelled_out_in_full(self):
        """``&copy;`` is six characters to Slack, and the reader must see six.

        Only ``&amp;``, ``&lt;`` and ``&gt;`` come back as a character there,
        so every other ``&`` is an ampersand the label means literally - a host
        that spells one is a real citation case.
        """
        assert slack(f"[a&copy;.example]({PLAIN})") == f"<{PLAIN}|a&amp;copy;.example>"

    def test_a_label_that_looks_like_a_mention_is_delivered_as_text(self):
        """A label reading ``<@U123>`` is text somebody wrote, not a mention."""
        assert slack(f"[<@U123>]({PLAIN})") == f"<{PLAIN}|&lt;@U123&gt;>"

    def test_emphasis_in_a_label_survives_the_encoding(self):
        assert slack(f"[**bold** & <em>]({PLAIN})") == f"<{PLAIN}|*bold* &amp; &lt;em&gt;>"

    def test_the_destination_is_delivered_as_it_stands(self):
        """The address is not text: encoding it would open a different page."""
        query = "https://example.com/s?a=1&b=2"

        assert slack(f"[q]({query})") == f"<{query}|q>"
        assert slack(f"<{query}>") == f"<{query}>"

    def test_text_outside_a_link_keeps_the_behaviour_it_had(self):
        """Only the link wrapper is this change's boundary."""
        for text in ("a > b & c", "5 < 6", "plain &amp; text"):
            assert slack(text) == converter_alone(text)


class _TextOnlyClient:
    """A client that can only send text, and whose sends all succeed.

    The success matters: a fragment delivered here is reported as a delivered
    result, so nothing downstream would ever fall back to sending the whole
    thing another way.
    """

    def __init__(self):
        self.sent: list[str] = []
        self._next_id = 1

    def should_use_thread_for_reply(self):
        return False

    async def send_message(self, context, text, parse_mode=None, reply_to=None, subtext=None):
        self.sent.append(text)
        message_id = f"msg-{self._next_id}"
        self._next_id += 1
        return message_id


class _DiscordLikeClient(_TextOnlyClient):
    """Adds the native Markdown upload Discord implements."""

    def __init__(self, *, upload_id: str = "discord-file-1"):
        super().__init__()
        self.uploads: list[tuple[str, str]] = []
        self._upload_id = upload_id

    async def upload_markdown(self, context, title, content, filetype="markdown"):
        self.uploads.append((title, content))
        return self._upload_id


def discord_context() -> MessageContext:
    return MessageContext(user_id="U1", channel_id="C1", platform="discord")


def wechat_context() -> MessageContext:
    # ``context_token`` is spelled here so the adapter resolves the recipient
    # from the message instead of reading its on-disk token cache.
    return MessageContext(
        user_id="wechat-user",
        channel_id="wechat-user",
        platform="wechat",
        platform_specific={"context_token": "ctx-token"},
    )


def dispatcher_for(platform: str, client) -> ConsolidatedMessageDispatcher:
    return ConsolidatedMessageDispatcher(_StubController(platform=platform, im_client=client))


class TestTheSplitKeepsALinkWhole:
    """Where the shared splitter is allowed to draw a boundary.

    A result longer than one message is cut into several, and the cut used to
    be taken at whitespace with no idea what it was cutting. A boundary drawn
    through ``[label](url)`` delivers neither half: both chunks send fine, so
    nothing falls back, and the reader is shown raw Markdown and no way to
    reach the source.
    """

    def test_a_fitting_link_crossing_a_boundary_moves_whole(self):
        link = f"[a b]({PLAIN})"
        text = "x" * 1890 + link + " tail"

        chunks = dispatcher_for("discord", _DiscordLikeClient())._split_result_text(text, 1900)

        assert "".join(chunks) == text
        assert sum(chunk.count(link) for chunk in chunks) == 1
        assert all(len(chunk) <= 1900 for chunk in chunks)

    def test_adjacent_links_are_each_kept_whole(self):
        first, second = f"[a]({PLAIN})", f"[b c]({STAR})"
        text = "x" * (1900 - len(first) - 3) + first + second + " tail"

        chunks = dispatcher_for("discord", _DiscordLikeClient())._split_result_text(text, 1900)

        assert "".join(chunks) == text
        assert [chunk.count(first) for chunk in chunks] == [1, 0]
        assert [chunk.count(second) for chunk in chunks] == [0, 1]
        assert all(len(chunk) <= 1900 for chunk in chunks)

    def test_a_multibyte_budget_keeps_a_link_whole_and_within_the_limit(self):
        """WeChat counts bytes, so the boundary moves on a different ruler."""
        link = f"[文 档]({PLAIN})"
        text = "字" * 629 + link + "字" * 100

        chunks = dispatcher_for("wechat", _DiscordLikeClient())._split_result_text_by_bytes(text, 1900)

        assert "".join(chunks) == text
        assert sum(chunk.count(link) for chunk in chunks) == 1
        assert all(len(chunk.encode("utf-8")) <= 1900 for chunk in chunks)

    def test_a_link_longer_than_one_message_leaves_no_whole_boundary(self):
        """No cut keeps this one intact, and the plan says so instead of pretending."""
        dispatcher = dispatcher_for("discord", _DiscordLikeClient())
        oversized = f"[{'label ' * 400}]({PLAIN})"

        assert dispatcher._plan_result_split(f"lead\n\n{oversized}", 1900).links_whole is False
        assert dispatcher._plan_result_split_by_bytes(f"lead\n\n{oversized}", 1900).links_whole is False
        assert dispatcher._plan_result_split("x" * 4000, 1900).links_whole is True


class TestALinkTooLongToSplitIsDeliveredWhole(unittest.IsolatedAsyncioTestCase):
    """The consumer end: what the platform is actually asked to send.

    Persistence is stubbed out here - the workbench row is a different contract
    and writing one would reach real local state - so what remains is exactly
    the delivery the reader gets.
    """

    async def test_discord_attaches_the_whole_result_instead_of_fragmenting_a_link(self):
        client = _DiscordLikeClient()
        dispatcher = dispatcher_for("discord", client)
        text = f"Source:\n\n[{'label ' * 400}]({PLAIN})"

        with mock.patch("core.message_dispatcher.persist_agent_message"):
            message_id = await dispatcher.emit_agent_message(discord_context(), "result", text)

        assert client.uploads == [("result.md", text)]
        assert message_id == "discord-file-1"
        # Nothing of the result went out as text: no fragment, and no claim of
        # a delivery the reader could not follow.
        assert client.sent == [dispatcher._t("info.resultDeliveredAsAttachment")]

    async def test_discord_still_splits_when_every_link_fits(self):
        client = _DiscordLikeClient()
        dispatcher = dispatcher_for("discord", client)
        link = f"[a b]({PLAIN})"
        text = "x" * 1890 + link + " tail " + "y" * 1900

        with mock.patch("core.message_dispatcher.persist_agent_message"):
            message_id = await dispatcher.emit_agent_message(discord_context(), "result", text)

        assert message_id == "msg-1"
        assert client.uploads == []
        assert "".join(client.sent) == text
        assert sum(sent.count(link) for sent in client.sent) == 1

    async def test_a_platform_with_no_file_route_reports_no_delivery(self):
        """Honest failure beats a half link the user is told arrived."""
        client = _TextOnlyClient()
        dispatcher = dispatcher_for("discord", client)
        text = f"Source:\n\n[{'label ' * 400}]({PLAIN})"

        with mock.patch("core.message_dispatcher.persist_agent_message"):
            message_id = await dispatcher.emit_agent_message(discord_context(), "result", text)

        assert message_id is None
        assert client.sent == [dispatcher._t("error.resultDeliveryFailed")]


class TestTheWeChatAdapterCarriesTheWholeResult(unittest.IsolatedAsyncioTestCase):
    """The real adapter, with only its network calls replaced.

    WeChat inherits ``upload_markdown`` from ``BaseIMClient`` without
    implementing it - it raises - and implements ``upload_file_from_path``
    instead. A fallback that only knows the first name has no route here, which
    is exactly how a link too long to split used to go out in halves.
    """

    def _wechat(self):
        bot = WeChatBot(WeChatConfig(bot_token="token"))
        return bot, dispatcher_for("wechat", bot)

    @staticmethod
    def _text_items(calls: list) -> list[str]:
        return [
            item["text_item"]["text"]
            for call in calls
            for item in call
            if item.get("type") == 1
        ]

    async def _deliver(self, dispatcher, text, *, cdn_meta):
        calls: list = []
        uploaded: dict = {}

        async def fake_upload_to_cdn(base_url, token, cdn_base_url, user_id, file_path, proxy=None):
            uploaded["path"] = file_path
            uploaded["content"] = Path(file_path).read_text(encoding="utf-8")
            return cdn_meta

        async def fake_send_message(base_url, token, to_user_id, context_token, item_list, proxy=None):
            calls.append(item_list)
            return {"message_id": f"wc-{len(calls)}"}

        with (
            mock.patch.object(wechat_cdn, "upload_file_to_cdn", fake_upload_to_cdn),
            mock.patch.object(wechat_api, "send_message", fake_send_message),
            mock.patch("core.message_dispatcher.persist_agent_message"),
        ):
            message_id = await dispatcher.emit_agent_message(wechat_context(), "result", text)

        return message_id, calls, uploaded

    async def test_the_whole_result_rides_the_file_upload_the_adapter_has(self):
        bot, dispatcher = self._wechat()
        text = f"来源：\n\n[{'标签' * 600}]({PLAIN})"

        message_id, calls, uploaded = await self._deliver(
            dispatcher,
            text,
            cdn_meta={"encrypt_query_param": "q", "aes_key": "k", "file_size": 12, "file_id": "wc-file-1"},
        )

        assert message_id == "wc-file-1"
        assert uploaded["content"] == text
        assert Path(uploaded["path"]).name == "result.md"
        # The staging file is the transport's, not the machine's: it is gone
        # once the upload returns.
        assert not Path(uploaded["path"]).exists()
        # One file item, and the only text sent is the notice - no fragment of
        # the link was delivered before it.
        assert [item["type"] for call in calls for item in call].count(4) == 1
        assert self._text_items(calls) == [
            bot.format_markdown(dispatcher._t("info.resultDeliveredAsAttachment"))
        ]

    async def test_a_failed_upload_is_not_reported_as_a_delivery(self):
        """The CDN refuses, so ``upload_file_from_path`` answers with an empty id."""
        bot, dispatcher = self._wechat()
        text = f"来源：\n\n[{'标签' * 600}]({PLAIN})"

        message_id, calls, uploaded = await self._deliver(dispatcher, text, cdn_meta=None)

        assert message_id is None
        assert not Path(uploaded["path"]).exists()
        assert self._text_items(calls) == [
            bot.format_markdown(dispatcher._t("error.resultDeliveryFailed"))
        ]

    async def test_a_result_whose_links_all_fit_is_still_split_as_before(self):
        """Still split, and the chunk boundary now falls outside the link.

        WeChat renders the unit its own way - ``label (url)`` - so what has to
        survive the split is that rendering, whole and in one message.
        """
        bot, dispatcher = self._wechat()
        text = "字" * 629 + f"[文 档]({PLAIN})" + "字" * 400

        message_id, calls, uploaded = await self._deliver(
            dispatcher,
            text,
            cdn_meta={"file_id": "wc-file-1"},
        )

        assert message_id == "wc-1"
        assert uploaded == {}
        chunks = self._text_items(calls)
        assert len(chunks) > 1
        assert sum(chunk.count(bot.format_markdown(f"[文 档]({PLAIN})")) for chunk in chunks) == 1
        assert sum(chunk.count("字") for chunk in chunks) == 1029
        assert all(len(chunk.encode("utf-8")) <= 1900 for chunk in chunks)
