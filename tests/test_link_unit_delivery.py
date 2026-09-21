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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.reply_enhancer import inline_links
from modules.im.formatters import hold_links, restore_held
from modules.im.slack import SlackBot, SlackMarkdownConverter

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
    """What the third-party converter does with no protection at all."""
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

        assert converter_alone(text) == "<https://a_b_.example/x|^f]: [p>"
        assert slack(text) == f"[^f]: <{STAR}|p>"

    def test_an_html_block_keeps_the_address_it_holds(self):
        text = f"<div>\n[x]({STAR})\n</div>"

        assert "https://a_b_.example/x" in converter_alone(text)
        assert slack(text) == f"<div>\n<{STAR}|x>\n</div>"

    def test_a_table_cell_becomes_a_link_instead_of_raw_markdown(self):
        text = f"| Source |\n| --- |\n| [t]({STAR}) |"

        assert f"[t]({STAR})" in converter_alone(text)
        assert slack(text) == f"*Source*\n<{STAR}|t>"

    def test_a_label_written_across_two_lines_arrives_as_one_link(self):
        """A newline inside ``<url|label>`` is not a link, and a soft break is a space.

        The converter's link pattern never crosses a line, so on its own it
        left the Markdown standing - with the address already rewritten by the
        emphasis pass that does not stop at one either.
        """
        text = f"See [the\nguide]({STAR}) here."

        assert converter_alone(text) == "See [the\nguide](https://a_b_.example/x) here."
        assert slack(text) == f"See <{STAR}|the guide> here."

    def test_a_titled_link_no_longer_sends_the_title_as_part_of_the_address(self):
        text = f'[docs]({PLAIN} "T")'

        assert converter_alone(text) == f'<{PLAIN} "T"|docs>'
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
