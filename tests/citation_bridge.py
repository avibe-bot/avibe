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

import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.citations import (  # noqa: E402
    _END,
    _SEP,
    _START,
    CitationSource,
    resolve_citations,
)
from core.reply_enhancer import inline_links, process_reply, unescape_markdown  # noqa: E402

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
        "key": "unresolved",
        "why": "A marker naming a source the backend never captured. It degrades "
        "to a visible label rather than vanishing, and writes no sidecar row.",
        "sources": {},
        "text": "Claimed. {m0}",
    },
)


def _resolve(text: str, sources: Mapping[str, CitationSource]):
    return resolve_citations(text, sources, unresolved_label=UNRESOLVED_LABEL)


def _spelling_probe(sources: Mapping[str, CitationSource], refs: list[str]) -> list[str]:
    """The exact Markdown link the producer writes for each ref, measured."""
    spellings: list[str] = []
    for ref in refs:
        _, sidecar = _resolve(marker(ref), sources)
        spellings.append(sidecar[0]["spelling"] if sidecar else "")
    return spellings


def _literal_occurrences(source: str, needle: str) -> list[int]:
    """Every offset of *needle*, scanned exactly the way the producer scans."""
    found: list[int] = []
    at = source.find(needle)
    while at != -1:
        found.append(at)
        at = source.find(needle, at + 1)
    return found


def _anchors(text: str, citations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The links a reader of *text* is shown, and which of them are citations.

    The labels and addresses come from the same inline-link scan the delivery
    pass runs, so they are what the product itself reads out of this text rather
    than what this file expects to be there. ``cited`` then applies the sidecar's
    stated rule - this exact spelling, this occurrence, this total - which is the
    rule a consumer has to reach the same answer with.

    This is a floor, not a census: the scan reads CommonMark, and a consumer may
    legitimately show links it does not see (a GFM footnote definition) or fewer
    (a ``file://`` attachment, which is not a web link on any surface). Each
    case records whether its anchors are exhaustive so a consumer test knows
    which of the two questions it may ask.
    """
    rows = [row for row in citations if row.get("spelling")]
    counted: dict[str, list[int]] = {}
    anchors: list[dict[str, Any]] = []
    for link in inline_links(text):
        label_end = link.destination_start - 2
        raw_label = text[link.start + 1 : label_end]
        spelling = text[link.start : link.destination_end + 1]
        if not spelling.endswith(")"):
            # A link with a title spells more than ``[label](dest)``; no citation
            # writes one, and no case here needs to describe one.
            spelling = ""
        occurrences = counted.get(spelling)
        if spelling and occurrences is None:
            occurrences = _literal_occurrences(text, spelling)
            counted[spelling] = occurrences
        ordinal = (occurrences.index(link.start) + 1) if occurrences else 0
        cited = any(
            row["spelling"] == spelling
            and row.get("occurrence_total") == len(occurrences or ())
            and ordinal in (row.get("occurrences") or ())
            for row in rows
        )
        anchors.append(
            {
                "label": unescape_markdown(raw_label),
                "url": link.destination,
                "cited": cited,
            }
        )
    return anchors


def build() -> dict[str, Any]:
    """Run the real producer and delivery pass over every case."""
    cases: list[dict[str, Any]] = []
    for case in CASES:
        refs = sorted(case["sources"])
        sources = {
            ref: source(ref, url, title) for ref, (url, title) in case["sources"].items()
        }
        markers = {f"m{n}": marker(ref) for n, ref in enumerate(refs)} or {"m0": marker("turn0view0")}
        links = {f"link{n}": spelling for n, spelling in enumerate(_spelling_probe(sources, refs))}
        agent_text = case["text"].format(**markers, **links)

        delivered, citations = _resolve(agent_text, sources)
        # The two surfaces, reached exactly as ``core.message_dispatcher`` reaches
        # them: the workbench keeps its file links so it can rewrite them for
        # inline rendering, IM gets them flattened to plain labels.
        web = process_reply(delivered, keep_file_links=True)
        im = process_reply(delivered)
        cases.append(
            {
                "key": case["key"],
                "why": case["why"],
                "agent_text": agent_text,
                "delivered": delivered,
                "citations": citations,
                "web_text": web.text,
                "im_text": im.text,
                "quick_replies": [button.text for button in web.buttons],
                "files": [{"label": f.label, "path": f.path} for f in web.files],
                "web_anchors": _anchors(web.text, citations),
                "im_anchors": _anchors(im.text, citations),
                # The scan above reads this text as CommonMark; the Web renderer
                # adds GFM on top. A footnote definition is the one construct
                # here that holds a link CommonMark does not see - and it is
                # also rendered where the footnotes go rather than where it was
                # written - so a text containing one may be asked which links it
                # must show, but not "and nothing else, in this order".
                "anchors_exhaustive": "[^" not in web.text,
            }
        )
    return {"generated_by": "tests/citation_bridge.py", "cases": cases}


def main() -> None:
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE}")


if __name__ == "__main__":
    main()
