"""One producer run every citation consumer is measured against.

A citation is written by ``core.citations`` in Python and recognized again by
``ui/src/lib/citations.ts`` in TypeScript, over two different Markdown parsers,
after the delivery pass in ``core.reply_enhancer`` has had its turn. Each of
those three boundaries is a place the two ends can quietly stop agreeing, and a
test that hand-writes what the backend "would have" emitted cannot see it
happen: it measures the expectation, not the product.

So this module runs the real producer once and writes what it actually emitted -
the delivered text, the sidecar, and the text each surface is really handed -
into ``tests/fixtures/citation_consumer_bridge.json``. Both consumer suites read
that one file: ``tests/test_citation_consumers.py`` drives the real Slack,
Telegram, WeChat, Discord and Feishu renderers with it, and
``ui/src/components/ui/citation-bridge.test.tsx`` drives the real ``Markdown``
component with it. When the producer changes, one regeneration moves both.

The expectations travel with it, and they are derived rather than authored: the
anchors a case should end up showing are read back out of the delivered text
with the same ``inline_links`` the delivery pass itself uses, and each one is
marked cited by the provenance rule the sidecar states. A consumer test then
asks a real renderer whether the reader sees exactly those labels pointing at
exactly those addresses - which is a question about the renderer, because the
answer it is checked against came from the producer, not from this file.

Regenerate with::

    .venv/bin/python -m tests.citation_bridge
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from core.citations import (  # noqa: E402
    _END,
    _SEP,
    _START,
    CitationSource,
    body_digest,
    finalize_citations,
    materialize_citations,
    register_citations,
    resolve_citations,
)
from core.message_dispatcher import ConsolidatedMessageDispatcher  # noqa: E402
from core.reply_enhancer import (  # noqa: E402
    inline_links,
    markdown_link_units,
    process_reply,
    unescape_markdown,
)
from modules.im import MessageContext  # noqa: E402
from storage.db import create_sqlite_engine, dispose_cached_sqlite_engines  # noqa: E402
from storage.importer import ensure_sqlite_state  # noqa: E402
from storage.models import agent_sessions, messages  # noqa: E402
from storage.settings_service import upsert_scope  # noqa: E402
from tests.test_message_dispatcher_platform_limits import _StubController  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "citation_consumer_bridge.json"

# What an unresolvable marker degrades to. The real backend passes a localized
# string; the exact wording is not what any of this is measuring.
UNRESOLVED_LABEL = "(source unavailable)"


def marker(*refs: str) -> str:
    """One citation marker naming *refs*, spelled the way Codex spells it."""
    return f"{_START}cite{_SEP}{_SEP.join(refs)}{_END}"


def source(ref: str, url: str, title: str) -> CitationSource:
    return CitationSource(ref_id=ref, url=url, title=title)


# Each case is one agent reply. ``text`` is what the model wrote, before any
# Avibe pass touched it; ``{m0}``/``{m1}`` are citation markers and ``{link0}``
# is the complete Markdown link the producer will write for the first source -
# substituted from a probe run, so a case can repeat a citation's own exact
# spelling without this file guessing what that spelling is.
CASES: tuple[dict[str, Any], ...] = (
    {
        "key": "star_host",
        "why": "A host holding Markdown emphasis: the label must read as the host "
        "it names, and the address must arrive character for character.",
        "sources": {"turn0view0": ("https://a*b*.example/x", "Star Host")},
        "text": "Cited. {m0}",
    },
    {
        "key": "entity_host",
        "why": "A host that spells a character reference once decoded; the reader "
        "must see the reference, not whatever it would expand to.",
        "sources": {"turn0view0": ("https://a%26copy%3B.example/x", "Entity Host")},
        "text": "Cited. {m0}",
    },
    {
        "key": "underscore_path",
        "why": "Underscores in the path and the query - not CommonMark emphasis, "
        "but Slack mrkdwn and Telegram both read them as italics.",
        "sources": {"turn0view0": ("https://example.com/a_b_c?q=_u_&r=1", "Underscores")},
        "text": "Cited. {m0}",
    },
    {
        "key": "backtick_query",
        "why": "A backtick and an asterisk inside the query string, where a "
        "converter that scans destinations rewrites the address itself.",
        "sources": {"turn0view0": ("https://example.com/p?q=%60code%60&r=a*b*", "Query")},
        "text": "Cited. {m0}",
    },
    {
        "key": "format_chars",
        "why": "A zero-width space in the query, percent-encoded: invisible "
        "characters must not silently drop out of an address.",
        "sources": {"turn0view0": ("https://example.com/p?q=a%E2%80%8Bb", "Invisible")},
        "text": "Cited. {m0}",
    },
    {
        "key": "ipv6_port",
        "why": "A literal IPv6 authority with a port. The brackets are reserved "
        "authority syntax, and a link nobody can open is not attribution.",
        "sources": {"turn0view0": ("https://[::1]:8443/x?q=1", "Loopback")},
        "text": "Cited. {m0}",
    },
    {
        "key": "paren_path",
        "why": "Parentheses in the address, balanced, unbalanced and already "
        "percent-encoded, one of them beside a literal IPv6 host. Each is the "
        "page its source named, so the URL keeps it as written; only the link "
        "guards it, and every reader has to land on the same address.",
        "sources": {
            "turn0view0": ("https://en.wikipedia.org/wiki/Foo_(bar)", "Balanced"),
            "turn0view1": ("https://example.com/a_(b?q=c)d", "Unbalanced"),
            "turn0view2": ("https://example.com/a%28b%29", "Encoded"),
            "turn0view3": ("https://[::1]:8443/x(y", "Loopback"),
        },
        "text": "One {m0}, two {m1}, three {m2}, four {m3}.",
    },
    {
        "key": "footnote",
        "why": "A GFM footnote definition holding a link to the same page. One "
        "renderer reads a link there and the other reads a definition, which is "
        "exactly the disagreement that cost the real citation its badge.",
        "sources": {"turn0view0": ("https://example.com/x", "Footnoted")},
        "text": "[^f]: [p](https://example.com/x)\n\nCited. {m0} [^f]",
    },
    {
        "key": "code_fence",
        "why": "The citation's own exact spelling shown as a code example. It is "
        "not a link to any reader, but it is the same characters, and both ends "
        "have to count it the same way.",
        "sources": {"turn0view0": ("https://example.com/x", "Fenced")},
        "text": "```\n{link0}\n```\n\nCited. {m0}",
    },
    {
        "key": "table",
        "why": "The same spelling inside a GFM table cell, where `|` ends a cell "
        "and a label that spells one would split the row.",
        "sources": {"turn0view0": ("https://example.com/x", "Tabled")},
        "text": "| Source |\n| --- |\n| {link0} |\n\nCited. {m0}",
    },
    {
        "key": "near_html",
        "why": "The same spelling next to inline HTML, which each renderer is "
        "free to keep, escape or drop.",
        "sources": {"turn0view0": ("https://example.com/x", "Inline HTML")},
        "text": "Cited. {m0} <span>{link0}</span>",
    },
    {
        "key": "prose_repeat",
        "why": "The agent points at the cited page in its own prose, before and "
        "after, spelled identically. Only the middle one is a citation, and an "
        "ordinary link must never be impersonated.",
        "sources": {"turn0view0": ("https://example.com/x", "Repeated")},
        "text": "Background: {link0}\n\nCited. {m0}\n\nAlso {link0}",
    },
    {
        "key": "emoji_prefix",
        "why": "Non-ASCII text before the marker: offsets counted in characters "
        "on one side and in UTF-16 units on the other stop agreeing here.",
        "sources": {"turn0view0": ("https://example.com/x", "Emoji")},
        "text": "🔎 出处 {m0}",
    },
    {
        "key": "silent_strip",
        "why": "A silent control block holding the same spelling. Delivery removes "
        "it, so the consumer never counts it - and neither may the producer.",
        "sources": {"turn0view0": ("https://example.com/x", "Silent")},
        "text": "<silent>note to self: {link0}</silent>\nCited. {m0}",
    },
    {
        "key": "file_link",
        "why": "A file:// attachment link in the same reply: IM strips it to a "
        "plain label and the workbench keeps it, so the two surfaces get "
        "different text and the citation has to survive both.",
        "sources": {"turn0view0": ("https://example.com/x", "With attachment")},
        "text": "Cited. {m0}\n\n[report](file:///tmp/avibe-citation-bridge/report.txt)",
    },
    {
        "key": "quick_replies",
        "why": "A trailing quick-reply block, which delivery strips from the body "
        "on both surfaces.",
        "sources": {"turn0view0": ("https://example.com/x", "With buttons")},
        "text": "Cited. {m0}\n\n---\n[👌 继续] | [✅ 完成]",
    },
    {
        "key": "marker_shaped",
        "why": "The workbench rewrites `$<NAME>` into a secure-input card and "
        "`@<\u2026>`/`#<\u2026>` into chips by editing the source text before Markdown "
        "parses it - so a reply carrying those markers, and a source address "
        "spelled like one, is where an inserted card could move or swallow the "
        "link the producer counted.",
        "sources": {
            "turn0view0": ("https://example.com/p?q=$<openAiKey>&a=@<claude>", "Marker Shaped"),
        },
        "text": "Provide $<openAiKey>, then ping @<claude> in #<ses6jr7c5h2q6>.\n\nCited. {m0}",
    },
    {
        "key": "multi_source",
        "why": "Two sources in one reply, each with its own index and its own "
        "spelling.",
        "sources": {
            "turn0view0": ("https://example.com/x", "First"),
            "turn0view1": ("https://a*b*.example/y", "Second"),
        },
        "text": "First {m0}, second {m1}.",
    },
    {
        "key": "link_label",
        "why": "The marker written inside a link's own label. A citation is a "
        "link, and a link inside a link is not one - CommonMark makes the "
        "outer brackets text again - so the source is shown after the unit "
        "that encloses it, with that unit's destination and the label's other "
        "words untouched. Both links name the same page here, which is where "
        "a binding that matched on the address would badge the wrong one.",
        "sources": {"turn0view0": ("https://openai.com/docs", "OpenAI Docs")},
        "text": "See [the docs {m0}](https://openai.com/docs) for details.",
    },
    {
        "key": "link_label_positions",
        "why": "One source cited from inside three labels - opening one, in "
        "the middle of the next, closing the third. One row, three spans, and "
        "each source after its own unit rather than after the last of them.",
        "sources": {"turn0view0": ("https://example.com/s", "Thrice")},
        "text": "[{m0} a](https://u.example/1), [b {m0} c](https://u.example/2), "
        "[d {m0}](https://u.example/3).",
    },
    {
        "key": "data_slots",
        "why": "The slots of a link that are address or syntax rather than "
        "anything a reader is shown: a destination, a title, an angle-bracketed "
        "destination, and an autolink, which is all address. A marker there is "
        "part of what makes the unit work, so it stays the characters the model "
        "typed - no token minted, no source fetched, no link spliced in - while "
        "the one in the prose beside them is still a citation.",
        "sources": {"turn0view0": ("https://example.com/s", "Prose")},
        "text": "Cited. {m0}\n\n"
        "[a](https://example.com/{m0}path) [b](https://example.com/y \"T {m0}\") "
        "[c](<https://example.com/z{m0}>) <https://example.com/auto{m0}>",
    },
    {
        "key": "reference_slots",
        "why": "A reference link written in full shows its label and names its "
        "definition separately, so the label is a place a citation can go. "
        "Written short, the same brackets are both - the text a reader sees and "
        "the identifier that finds the address - so editing them would leave a "
        "link pointing at nothing. Those stay exactly as written, definition "
        "included.",
        "sources": {"turn0view0": ("https://example.com/s", "Referenced")},
        "text": "[the docs {m0}][ref], [dual {m0}] and [dual {m0}][].\n\n"
        "[ref]: https://r.example/page\n[dual {m0}]: https://r.example/dual",
    },
    {
        "key": "image_slots",
        "why": "An image's alt text is what a reader is given when the picture "
        "is not there; its src is an address. One marker is a citation and the "
        "other is not, in one line. The component does not fetch a foreign "
        "image - it renders a click-through link instead - so both are links on "
        "this surface.",
        "sources": {"turn0view0": ("https://example.com/s", "Illustrated")},
        "text": "![a diagram {m0}](https://i.example/p.png) and "
        "![plain](https://i.example/{m0}q.png)",
    },
    {
        "key": "image_in_link",
        "why": "An image inside a link: two units, one holding the other. The "
        "source belongs after the outer one. Placed after the image instead it "
        "would sit in the link's label and take the link down with it.",
        "sources": {"turn0view0": ("https://example.com/s", "Nested")},
        "text": "[![a diagram {m0}](https://i.example/p.png)](https://out.example/page)",
    },
    {
        "key": "moved_by_a_transform",
        "why": "Delivery removes silent blocks, and what closes up behind them "
        "leaves a registered token somewhere the model never put it. Each one "
        "is judged where it ends up rather than where it started: inside a "
        "label it is still a citation and is shown after that link, inside a "
        "destination or a title it is data again and the marker the model typed "
        "comes back - the alternative is a link nobody can open and a source "
        "claimed where no reader can see it.",
        "sources": {"turn0view0": ("https://example.com/s", "Moved")},
        "text": "A [lab <silent>](https://z.example) x</silent>{m0}](https://out.example/page) B"
        "\n\nC [two](https://z.example<silent>) y</silent>{m0}) D"
        "\n\nE [three](https://z.example \"t<silent>\") y</silent>{m0}\") F",
    },
    {
        "key": "open_fence",
        "why": "A fence the answer never closed takes the rest of the reply "
        "into code. The citation's own spelling in there is not a link to any "
        "renderer here, and a marker in there is not a citation either - which "
        "is the same rule the closed fence above gets, decided by the parser "
        "rather than by whether a closing line happens to exist.",
        "sources": {"turn0view0": ("https://example.com/s", "Unclosed")},
        "text": "Cited. {m0}\n\n```\n{link0}\n{m0} still open",
    },
    {
        "key": "file_label_and_quick_reply",
        "why": "A marker in an attachment's label and in a quick-reply button. "
        "Neither is Markdown a reader parses - one titles a file card, the "
        "other is a button - so the attribution is plain text left where it "
        "stands, with no link syntax and no token reaching a structured field. "
        "In the body the attachment is still a link, and CommonMark refuses "
        "``file:`` outright: a scan that cannot see that unit would put the "
        "source inside its label and break the card.",
        "sources": {"turn0view0": ("https://example.com/s", "Attached")},
        "text": "Cited. {m0}\n\n[report {m0}](file:///tmp/avibe-citation-bridge/report.txt)"
        "\n\n---\n[\U0001f44c \u7ee7\u7eed {m0}] | [\u2705 \u5b8c\u6210]",
    },
    {
        "key": "splice",
        "why": "Stripping a silent block splices two ordinary halves into "
        "characters that read as a citation marker. Only the first marker here "
        "was ever one, and the delivered text has to say so.",
        "sources": {"turn0view0": ("https://example.com/x", "Spliced")},
        "text": "Actual. {m0}\n\nSynthetic. \ue200ci<silent>private</silent>te"
        "\ue202turn0view0\ue201",
    },
    {
        "key": "file_delete_splice",
        "why": "Two deletions that cancel out. Removing the attachment link to "
        "its label and removing the silent block leave characters that read as "
        "a marker on IM and do not on the workbench - one reply, two bodies, "
        "and a marker shape that was never in the input either was.",
        "sources": {"turn0view0": ("https://example.com/x", "Cancelled out")},
        "text": "Actual. {m0}\n\nSynthetic. \ue200ci"
        "[te](file:///tmp/avibe-citation-bridge/report.txt)"
        "\ue202turn0view0<silent>note</silent>\ue201",
    },
    {
        "key": "file_link_above",
        "why": "A file:// attachment above the citation: IM flattens it to a "
        "label and the workbench keeps it, so one reply puts the same citation "
        "at two different offsets and each body needs its own measurement.",
        "sources": {"turn0view0": ("https://example.com/x", "Shifted")},
        "text": "[report](file:///tmp/avibe-citation-bridge/report.txt)\n\nCited. {m0}",
    },
    {
        "key": "unresolved",
        "why": "A marker naming a source the backend never captured. It degrades "
        "to a visible label rather than vanishing, and writes no sidecar row.",
        "sources": {},
        "text": "Claimed. {m0}",
    },
)


def _resolve(text: str, sources: Mapping[str, CitationSource]):
    return resolve_citations(text, sources, unresolved_label=UNRESOLVED_LABEL)


def _link_probe(sources: Mapping[str, CitationSource], refs: list[str]) -> list[str]:
    """The exact Markdown link the producer writes for each ref, measured."""
    spellings: list[str] = []
    for ref in refs:
        body, sidecar = _resolve(marker(ref), sources)
        spellings.append(utf16_slice(body, *sidecar[0]["spans"][0]) if sidecar else "")
    return spellings


def utf16_len(text: str) -> int:
    """The length of *text* in UTF-16 code units, the unit a span counts in."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def utf16_slice(text: str, start: int, end: int) -> str:
    """``text[start:end]`` read in UTF-16 code units.

    A Python slice counts code points, so the two disagree from the first
    non-BMP character onward - and a test that asserts what a span covers by
    slicing the string directly asserts the wrong characters exactly where it
    matters most.
    """
    units = text.encode("utf-16-le", "surrogatepass")
    return units[start * 2 : end * 2].decode("utf-16-le", "surrogatepass")


def _anchors(text: str, citations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The links a reader of *text* is shown, and which of them are citations.

    The labels and addresses come from the same inline-link scan the delivery
    pass runs, so they are what the product itself reads out of this text rather
    than what this file expects to be there. ``cited`` then applies the rule the
    sidecar states about THIS text: the row was measured in a body with this
    digest, and this link occupies one of the spans it names. A consumer has to
    reach the same answer the same way.

    This is a floor, not a census: the scan reads CommonMark, and a consumer may
    legitimately show links it does not see (a GFM footnote definition) or fewer
    (a ``file://`` attachment, which is not a web link on any surface). Each
    case records whether its anchors are exhaustive so a consumer test knows
    which of the two questions it may ask.
    """
    digest = body_digest(text)
    spans = {
        (span[0], span[1])
        for row in citations
        if row.get("body_sha256") == digest
        for span in row.get("spans") or ()
    }
    inner = _nested_unit_spans(text)
    anchors: list[dict[str, Any]] = []
    for link in inline_links(text):
        # A citation's span covers the whole link it wrote, so a link that
        # spells anything more - a title, say - simply matches no span.
        cited = (
            utf16_len(text[: link.start]),
            utf16_len(text[: link.end]),
        ) in spans
        # A label holding an image is not a string of text, and what a renderer
        # puts inside that anchor is the picture rather than the Markdown that
        # spelled it. Recording the source here would be recording something no
        # reader is shown, so the label is left unsaid and the link is known by
        # its address alone.
        label = unescape_markdown(text[link.label_start : link.label_end])
        if any(
            link.label_start <= start and end <= link.label_end for start, end in inner
        ):
            label = None
        anchors.append({"label": label, "url": link.destination, "cited": cited})
    return anchors


def _nested_unit_spans(text: str) -> list[tuple[int, int]]:
    """The spans of every link or image written inside another one."""
    units = markdown_link_units(text)
    return [
        (unit.start, unit.end)
        for unit in units
        if any(
            other is not unit and other.start <= unit.start and unit.end <= other.end
            for other in units
        )
    ]


# A link reference definition, and the address it names. Every other address a
# renderer can reach is spelled inside a unit the scan below already walks.
_DEFINITION = re.compile(r"^ {0,3}\[[^\]\n]*\]:[ \t]*(\S+)", re.MULTILINE)
_ADDRESS = re.compile(r"https?://[^\s<>()\[\]\"']+")


def _addresses_cover_the_surface(text: str, anchors: list[dict[str, Any]]) -> bool:
    """Whether an address a reader of *text* reaches must be one of *anchors*.

    ``inline_links`` reads inline links, which is what the delivery pass needs
    and less than a reader gets: an image, a reference link or an autolink is a
    link to a reader and not to that scan, and a reference definition holds an
    address no unit spells. So everything it did not resolve is read back for
    the addresses it spells - an attachment names none, and neither does a
    reference that only points at a definition - and each one has to be an
    address already recorded. When it is not, the anchors are a floor: a
    consumer may be shown somewhere this file does not name, so it may be asked
    which links it must show and never which it must not.
    """
    recorded = {anchor["url"] for anchor in anchors}
    scanned = {(link.start, link.end) for link in inline_links(text)}
    unresolved = [
        text[unit.start : unit.end]
        for unit in markdown_link_units(text)
        if (unit.start, unit.end) not in scanned
    ]
    unresolved.extend(match.group(1) for match in _DEFINITION.finditer(text))
    return all(
        address in recorded
        for spelled in unresolved
        for address in _ADDRESS.findall(spelled)
    )


# ----- The rows the dispatcher actually stored ------------------------------
#
# The cases above stop where the delivery pass does: at the text each surface is
# handed. A transcript row is one step further on, and it is not that text. The
# dispatcher folds a result footer into the body it stores for IM and keeps it
# beside the body it stores for the workbench, and the Web reader then splits
# the footer back off before Markdown ever sees the body. That split is a real
# edit to the exact body the ranges were measured in, so a consumer that cannot
# carry a measurement across an edit loses the badge on every ordinary IM
# transcript row - which is a regression a fixture stopping at the delivered
# text could not see.
#
# So these rows are read back out of a real SQLite home after a real
# ``ConsolidatedMessageDispatcher`` wrote them. Nothing here is composed by this
# file; it only says which reply was sent and on which surface.

ROW_SESSION = "ses_bridge_rows"
ROW_NOW = "2026-09-21T12:00:00Z"
ROW_SOURCES = {"turn0view0": ("https://example.com/x", "Cited page")}
# Prose pointing at the cited page in the answer's own words, then the citation
# itself - the same spelling twice, so a consumer that loses the measurement and
# falls back to matching text would badge the wrong one and say so. The emoji
# ahead of both is a surrogate pair: it makes a range counted in characters and
# a range counted in UTF-16 code units disagree from the first link onward.
ROW_TEXT = "🔎 Background: {link0}\n\nCited. {m0}"
# The same answer carrying every transform a stored body can stack above its
# citation: an attachment the two surfaces rewrite differently, and a secret
# marker the reader turns into a card as it renders.
ATTACHED_AND_CARDED = (
    "[report](file:///tmp/avibe-citation-bridge/report.txt)\n\n"
    "Provide $<deployKey> first.\n\n" + ROW_TEXT
)

ROW_CASES: tuple[dict[str, Any], ...] = (
    {
        "key": "im_row_with_footer",
        "why": "An ordinary IM transcript row. The dispatcher folds the footer "
        "into the stored body and stores it structured as well, so the Web "
        "reader removes a trailing range before rendering - the case where "
        "adding a body digest could have cost every IM row its badge.",
        "platform": "slack",
        "type": "result",
        "footer": "✅ ⏱️ 5s · 🪙 1.2k tok",
    },
    {
        "key": "im_row_without_footer",
        "why": "The same row with nothing to strip: the body the ranges were "
        "measured in is the body the renderer is handed.",
        "platform": "slack",
        "type": "result",
        "footer": None,
    },
    {
        "key": "workbench_row_with_footer",
        "why": "The workbench stores a clean body and its footer apart, so the "
        "same reply reaches the same reader through a different split.",
        "platform": "avibe",
        "type": "result",
        "footer": "✅ ⏱️ 2m 24s",
    },
    {
        "key": "activity_row",
        "why": "An interim narration row, which the activity card renders "
        "verbatim - no footer, no split, and its own consumer.",
        "platform": "avibe",
        "type": "assistant",
        "footer": None,
    },
    {
        "key": "workbench_row_rewritten_and_carded",
        "why": "Every transform a stored row can stack, in the order they "
        "happen and all of them above the citation: an attachment link is "
        "rewritten to the media proxy before the sidecar is measured, a footer "
        "is folded in and stripped back out by the reader, and a `$<NAME>` "
        "marker becomes a secure-input card as the reader renders it. Each one "
        "moves the citation, and only one of them was measured by the backend.",
        "platform": "avibe",
        "type": "result",
        "footer": "✅ ⏱️ 2m 24s",
        "text": ATTACHED_AND_CARDED,
    },
    {
        "key": "im_row_flattened_carded_and_folded",
        "why": "The same reply down the IM path, where the transforms stack "
        "differently: the attachment is flattened to a bare label instead of a "
        "proxy link, and the footer is folded into the stored body. So the "
        "reader runs two edits over one body - a trailing strip and an "
        "inserted secure-input card above the citation - and the badge has to "
        "survive their composition.",
        "platform": "slack",
        "type": "result",
        "footer": "✅ ⏱️ 5s",
        "text": ATTACHED_AND_CARDED,
    },
)


def _row_reply(sources: Mapping[str, CitationSource], text: str = ROW_TEXT) -> str:
    refs = sorted(sources)
    return text.format(
        **{f"m{n}": marker(ref) for n, ref in enumerate(refs)},
        **{f"link{n}": spelling for n, spelling in enumerate(_link_probe(sources, refs))},
    )


def _seed_home() -> None:
    """A temporary Avibe home holding one session the rows can belong to."""
    dispose_cached_sqlite_engines()
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_bridge_rows",
                now=ROW_NOW,
            )
            conn.execute(
                agent_sessions.insert().values(
                    id=ROW_SESSION,
                    scope_id=scope_id,
                    agent_backend="codex",
                    agent_variant="default",
                    session_anchor=f"anchor_{ROW_SESSION}",
                    native_session_id="",
                    status="active",
                    metadata_json="{}",
                    created_at=ROW_NOW,
                    updated_at=ROW_NOW,
                    last_active_at=ROW_NOW,
                )
            )
    finally:
        engine.dispose()


def _stored_rows(seen: set[str]) -> list[dict[str, Any]]:
    """Rows this store has gained since the last call, oldest first."""
    engine = create_sqlite_engine()
    try:
        with engine.connect() as conn:
            fresh = [
                {"type": row[1], "text": row[2], "content": json.loads(row[3] or "null")}
                for row in conn.execute(
                    select(
                        messages.c.id,
                        messages.c.type,
                        messages.c.content_text,
                        messages.c.content_json,
                    ).order_by(messages.c.id)
                )
                if row[0] not in seen and not seen.add(row[0])
            ]
    finally:
        engine.dispose()
    return fresh


def build_rows() -> list[dict[str, Any]]:
    """Drive the real dispatcher once per surface and read the rows back."""
    sources = {ref: source(ref, url, title) for ref, (url, title) in ROW_SOURCES.items()}
    rows: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as home:
        with mock.patch.dict(os.environ, {"AVIBE_HOME": home}):
            _seed_home()
            seen: set[str] = set()
            for case in ROW_CASES:
                reply = _row_reply(sources, case.get("text", ROW_TEXT))
                registered, bundle = register_citations(
                    reply, sources, unresolved_label=UNRESOLVED_LABEL
                )
                controller = _StubController(case["platform"])
                controller.config.reply_enhancements = True
                context = MessageContext(
                    user_id="workbench" if case["platform"] == "avibe" else "u1",
                    channel_id=ROW_SESSION if case["platform"] == "avibe" else "c1",
                    platform=case["platform"],
                    platform_specific=(
                        {"agent_session_id": ROW_SESSION}
                        if case["platform"] == "avibe"
                        else None
                    ),
                )
                asyncio.run(
                    ConsolidatedMessageDispatcher(controller).emit_agent_message(
                        context,
                        case["type"],
                        registered,
                        result_footer=case["footer"],
                        citations=bundle,
                    )
                )
                written = _stored_rows(seen)
                # One emit is one logical message, so one row. A surface that
                # wrote none (a send that failed, a type that is not stored)
                # would otherwise be recorded as a case with nothing in it.
                if len(written) != 1:
                    raise AssertionError(f"{case['key']} wrote {len(written)} rows, expected 1")
                rows.append(
                    {
                        **{
                            k: v
                            for k, v in case.items()
                            if k not in ("footer", "text")
                        },
                        "row": written[0],
                    }
                )
            dispose_cached_sqlite_engines()
    return rows


def build() -> dict[str, Any]:
    """Run the real producer and delivery pass over every case."""
    cases: list[dict[str, Any]] = []
    for case in CASES:
        refs = sorted(case["sources"])
        sources = {
            ref: source(ref, url, title) for ref, (url, title) in case["sources"].items()
        }
        markers = {f"m{n}": marker(ref) for n, ref in enumerate(refs)} or {"m0": marker("turn0view0")}
        links = {f"link{n}": spelling for n, spelling in enumerate(_link_probe(sources, refs))}
        agent_text = case["text"].format(**markers, **links)

        # Registration happens where the markers still mean what they say, and
        # what travels from here is an opaque token per marker. The tokens are
        # nonces, so nothing recorded below may contain one: this file records
        # the bodies each surface finally shows.
        registered, bundle = register_citations(
            agent_text, sources, unresolved_label=UNRESOLVED_LABEL
        )
        # The two surfaces, reached exactly as ``core.message_dispatcher`` reaches
        # them: the workbench keeps its file links so it can rewrite them for
        # inline rendering, IM gets them flattened to plain labels. Each body is
        # then written separately, because they are different bodies.
        web = process_reply(registered, keep_file_links=True)
        im = process_reply(registered)
        web_text, web_citations = finalize_citations(web.text, bundle)
        # IM carries no sidecar; its rows are measured here only to say which of
        # its links a reader is owed as a citation.
        im_text = materialize_citations(im.text, bundle)
        im_citations = finalize_citations(im.text, bundle)[1]
        web_anchors = _anchors(web_text, web_citations)
        im_anchors = _anchors(im_text, im_citations)
        cover = _addresses_cover_the_surface(
            web_text, web_anchors
        ) and _addresses_cover_the_surface(im_text, im_anchors)
        cases.append(
            {
                "key": case["key"],
                "why": case["why"],
                "sources": {
                    ref: {"url": url, "title": title}
                    for ref, (url, title) in case["sources"].items()
                },
                "agent_text": agent_text,
                "web_citations": web_citations,
                "im_citations": im_citations,
                "web_text": web_text,
                "im_text": im_text,
                "quick_replies": [
                    materialize_citations(button.text, bundle, as_markdown=False)
                    for button in web.buttons
                ],
                "files": [
                    {
                        "label": materialize_citations(f.label, bundle, as_markdown=False),
                        "path": f.path,
                    }
                    for f in web.files
                ],
                "web_anchors": web_anchors,
                "im_anchors": im_anchors,
                # Two different questions, and a case may answer yes to the
                # first and no to the second. ``cover`` says the addresses below
                # are every address a reader is given, so a link pointing
                # anywhere else is a link nobody wrote. ``exhaustive`` says they
                # are also in the order they are shown - which a GFM footnote
                # definition breaks by itself, because its link is rendered
                # where the footnotes go rather than where it was written.
                "anchors_cover_destinations": cover,
                "anchors_exhaustive": cover and "[^" not in web_text,
            }
        )
    return {
        "generated_by": "tests/citation_bridge.py",
        "cases": cases,
        "rows": build_rows(),
    }


def main() -> None:
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE}")


if __name__ == "__main__":
    main()
