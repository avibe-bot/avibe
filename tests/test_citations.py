"""The shared citation grammar, sanitizer, and Markdown rewrite.

Fixture-driven on purpose: the marker shape and the ref_id vocabulary
(``turn0view0``, ``turn0view1``, ``turn1view0``) are the ones a real Codex
0.154.0 turn emitted, so a table entry here is a recorded observation rather
than an invented example.

Two properties carry the feature and are asserted separately below rather than
left implicit in the happy path:

* attribution is never silently deleted - an unresolvable marker degrades to a
  visible label, and with no label to fall back to the raw marker survives;
* a URL is never invented - an unknown ref, an unsafe scheme, and a hostless
  URL all resolve to nothing rather than to a guess.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.citations import (
    CitationSource,
    citation_ref_ids,
    clean_title,
    has_citation_markers,
    resolve_citations,
    safe_url,
    source_label,
    unresolved_refs,
)
from core.reply_enhancer import strip_silent_blocks
from markdown_it import MarkdownIt

# The reference parser for anything asserted about rendered Markdown, so a
# claim that a label breaks its own link is a measurement and not a reading.
MARKDOWN = MarkdownIt("commonmark")

START, SEP, END = "\ue200", "\ue202", "\ue201"
UNRESOLVED = "(source unavailable)"


def marker(*ref_ids: str) -> str:
    """The exact wire shape: U+E200 cite U+E202 ref [U+E202 ref ...] U+E201."""
    return f"{START}cite{SEP}{SEP.join(ref_ids)}{END}"


# The two sources a real logged turn produced, plus the loopback probe's.
GUIDE = CitationSource(
    ref_id="turn0view0",
    title="Web search - OpenAI API",
    url="https://developers.openai.com/api/docs/guides/tools-web-search",
)
GUIDE_AGAIN = CitationSource(ref_id="turn1view0", title=GUIDE.title, url=GUIDE.url)
PROBE = CitationSource(
    ref_id="turn0view1",
    title="Citation probe source",
    url="https://example.com/citation-probe-source",
)
SOURCES = {s.ref_id: s for s in (GUIDE, GUIDE_AGAIN, PROBE)}


def resolve(text: str, sources=None, *, unresolved_label: str = UNRESOLVED):
    return resolve_citations(
        text,
        SOURCES if sources is None else sources,
        unresolved_label=unresolved_label,
    )


class TestResolution:
    def test_real_marker_becomes_a_markdown_link_and_one_sidecar_entry(self):
        text, citations = resolve(f"Native search is documented.{marker('turn0view0')}")

        assert text == (
            "Native search is documented. "
            "[developers.openai.com](https://developers.openai.com/api/docs/guides/tools-web-search)"
        )
        assert citations == [
            {
                "index": 1,
                "ref_id": "turn0view0",
                "title": "Web search - OpenAI API",
                "url": "https://developers.openai.com/api/docs/guides/tools-web-search",
                "label": "developers.openai.com",
                # The complete link this citation wrote, and the one occurrence
                # of that exact spelling in the message that is its own. See
                # TestLinkProvenance.
                "spelling": (
                    "[developers.openai.com]"
                    "(https://developers.openai.com/api/docs/guides/tools-web-search)"
                ),
                "occurrences": [1],
                "occurrence_total": 1,
            }
        ]

    def test_multiple_refs_in_one_marker_each_become_a_link(self):
        text, citations = resolve(f"Two sources.{marker('turn0view0', 'turn0view1')}")

        assert text.endswith(
            "[developers.openai.com](https://developers.openai.com/api/docs/guides/tools-web-search) "
            "[example.com](https://example.com/citation-probe-source)"
        )
        assert [c["index"] for c in citations] == [1, 2]
        assert [c["ref_id"] for c in citations] == ["turn0view0", "turn0view1"]

    def test_index_follows_first_appearance_across_markers(self):
        _, citations = resolve(
            f"B{marker('turn0view1')} then A{marker('turn0view0')} then B again{marker('turn0view1')}"
        )

        assert [(c["index"], c["label"]) for c in citations] == [
            (1, "example.com"),
            (2, "developers.openai.com"),
        ]

    def test_two_refs_for_the_same_page_share_one_index(self):
        """``turn0view0`` and ``turn1view0`` are the same URL from two searches."""
        text, citations = resolve(f"First{marker('turn0view0')} second{marker('turn1view0')}")

        assert len(citations) == 1
        assert citations[0]["index"] == 1
        assert text.count("[developers.openai.com]") == 2

    def test_every_sidecar_entry_matches_a_link_the_text_actually_carries(self):
        """The Web renderer matches on (href, link text), so the pair must agree."""
        text, citations = resolve(
            f"A{marker('turn0view0')} B{marker('turn0view1')} C{marker('turn0view0', 'turn9view9')}"
        )

        for citation in citations:
            assert f"[{citation['label']}]({citation['url']})" in text

    def test_markers_survive_a_final_message_that_repeats_an_earlier_one(self):
        """Resolving twice is idempotent: the second pass sees no markers left."""
        first, citations = resolve(f"Answer.{marker('turn0view0')}")
        second, again = resolve(first)

        assert second == first
        assert again == []
        assert len(citations) == 1


class TestAttributionIsNeverDeleted:
    def test_unknown_ref_degrades_to_the_visible_label(self):
        text, citations = resolve(f"Claimed.{marker('turn7view7')}")

        assert text == f"Claimed. {UNRESOLVED}"
        assert citations == []

    def test_partially_resolved_marker_keeps_its_link_and_labels_the_rest_once(self):
        text, citations = resolve(f"Mixed.{marker('turn0view0', 'turn7view7', 'turn8view8')}")

        assert text == (
            "Mixed. "
            "[developers.openai.com](https://developers.openai.com/api/docs/guides/tools-web-search) "
            f"{UNRESOLVED}"
        )
        assert text.count(UNRESOLVED) == 1
        assert len(citations) == 1

    def test_raw_marker_survives_when_there_is_no_label_to_fall_back_to(self):
        raw = f"Claimed.{marker('turn7view7')}"

        text, citations = resolve(raw, unresolved_label="")

        assert text == raw
        assert citations == []

    def test_marker_with_an_empty_payload_is_reported_not_dropped(self):
        text, _ = resolve(f"Claimed.{START}cite{SEP}{END}")

        assert text == f"Claimed. {UNRESOLVED}"

    @pytest.mark.parametrize(
        "sources",
        [{}, {"turn0view0": CitationSource(ref_id="turn0view0")}],
        ids=["no-results-captured", "result-without-a-url"],
    )
    def test_missing_metadata_is_labelled_rather_than_guessed(self, sources):
        text, citations = resolve(f"Claimed.{marker('turn0view0')}", sources)

        assert text == f"Claimed. {UNRESOLVED}"
        assert citations == []


class TestUntrustedMetadata:
    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "mailto:someone@example.com",
            "avibe-secret:OPENAI_KEY",
            "http:///no-host",
            "https://",
            "  ",
            "not a url at all",
        ],
    )
    def test_unsafe_or_hostless_urls_are_rejected(self, url):
        assert safe_url(url) == ""

        text, citations = resolve(
            f"Claimed.{marker('turn0view0')}",
            {"turn0view0": CitationSource(ref_id="turn0view0", title="T", url=url)},
        )
        assert text == f"Claimed. {UNRESOLVED}"
        assert citations == []

    @pytest.mark.parametrize(
        "url",
        ["http://example.com/x", "https://example.com/x"],
    )
    def test_http_and_https_are_accepted_verbatim(self, url):
        assert safe_url(url) == url

    def test_an_uppercase_scheme_is_normalized_rather_than_carried(self):
        """A scheme is case-insensitive, but a renderer that only knows its
        lowercase spelling sees no link at all: Telegram delivered
        ``[example.com](HTTPS://Example.com/X)`` as raw Markdown."""
        assert safe_url("HTTPS://Example.com/X") == "https://Example.com/X"

    @pytest.mark.parametrize(
        "url",
        [
            "https://2130706433/p",
            "https://0x7f.1/p",
            "https://127.1/p",
            "https://017700000001/p",
        ],
        ids=["decimal", "hexadecimal", "shortened", "octal"],
    )
    def test_a_numeric_host_is_named_by_the_address_a_browser_reaches(self, url):
        """Every one of these opens the loopback address. Attributing the
        citation to the digits instead would hide that from the reader."""
        assert source_label(safe_url(url)) == "127.0.0.1"

    @pytest.mark.parametrize(
        "url",
        ["https://256.1.1.1/p", "https://1.2.3.4.5/p", "https://example.com.0x1/p"],
        ids=["out-of-range", "too-many-parts", "domain-shaped"],
    )
    def test_a_numeric_host_the_ipv4_parser_refuses_names_no_source(self, url):
        """A browser will not navigate to these, so there is no host to name."""
        assert safe_url(url) == ""

    @pytest.mark.parametrize(
        "code,replaced",
        [
            (0x08, True),
            (0x09, False),
            (0x0A, False),
            (0x0B, True),
            (0x0C, False),
            (0x0D, False),
            (0x0E, True),
            (0x1F, True),
            (0x20, False),
            (0x7E, False),
            (0x7F, True),
            (0x80, True),
            (0x9F, True),
            (0xA0, False),
            (0xD7FF, False),
            (0xD800, True),
            (0xDFFF, True),
            (0xE000, False),
            (0xFDCF, False),
            (0xFDD0, True),
            (0xFDEF, True),
            (0xFDF0, False),
            (0xFFFE, True),
            (0xFFFF, True),
            (0x1FFFE, True),
            (0x10FFFD, False),
            (0x10FFFF, True),
            (0x110000, True),
        ],
    )
    def test_the_replacement_table_is_the_one_the_renderer_uses(self, code, replaced):
        """micromark's table, boundary by boundary.

        The persisted URL has to equal the href drawn from it, so a numeric
        reference has to resolve the way the parser behind the Web renderer
        resolves it - which is not HTML's Windows-1252 mapping: ``&#x80;`` is
        U+FFFD there, never ``€``.
        """
        from core.citations import _is_replaced_code_point

        assert _is_replaced_code_point(code) is replaced

    def test_a_long_host_is_elided_from_the_left(self):
        """A label cut from the right reads as a site the link never opens."""
        host = "developers.openai.com." + "padding." * 6 + "attacker.example"

        label = source_label(f"https://{host}/x")

        assert label.startswith("…")
        assert label.endswith(".attacker.example")
        assert not label.startswith("developers.openai.com")
        assert host.endswith(label[1:])
        assert len(label) == 64

    def test_an_elision_shows_a_site_and_not_only_a_public_suffix(self):
        """``…co.uk`` is every site under it, which is where a long label hides.

        Keeping whole labels only was what produced that: the one label left of
        the suffix did not fit, so all of it was dropped. The budget is filled
        from the right instead, and the ellipsis marks the partial piece.
        """
        host = "a" * 62 + ".co.uk"

        label = source_label(f"https://{host}/x")

        assert label != "…co.uk"
        assert label == "…" + host[-63:]
        assert len(label) == 64

    @pytest.mark.parametrize(
        "host",
        [
            "a" * 70 + ".uk",
            "a" * 62 + ".co.uk",
            "a" * 100,
            "developers.openai.com." + "padding." * 6 + "attacker.example",
            "x" * 30 + "." + "y" * 30 + ".example.com",
        ],
    )
    def test_an_elided_label_is_always_a_tail_of_the_host(self, host):
        """The one invariant both surfaces assert: never a fabricated middle."""
        label = source_label(f"https://{host}/x")

        assert label.startswith("…")
        assert len(label) == 64
        assert host.endswith(label[1:])

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("p&Tab;q", "p%09q"),
            ("p&#x9;q", "p%09q"),
            ("p&#9;q", "p%09q"),
            ("p&#xA;q", "p%0Aq"),
            ("p&#10;q", "p%0Aq"),
            ("p&NewLine;q", "p%0Aq"),
            ("p&#xD;q", "p%0Dq"),
            # A literal one never reaches the renderer as part of a destination:
            # it ends the destination, so ``[x](...p<TAB>q)`` is not a link.
            ("p\tq", "pq"),
            ("p\nq", "pq"),
            ("p\rq", "pq"),
        ],
    )
    def test_a_tab_a_reference_spells_is_escaped_and_a_literal_one_is_dropped(
        self, raw, expected
    ):
        """The two are different characters, and the order of the passes decides.

        WHATWG's input cleanup removes a literal tab or newline, so it runs on
        the raw string. What a character reference resolves to afterwards is part
        of the destination, and micromark percent-encodes it - measured, not
        assumed: ``[x](https://example.com/p&Tab;q)`` renders
        ``https://example.com/p%09q``. Resolving before the cleanup deleted
        exactly those characters, silently moving the page.
        """
        assert safe_url(f"https://example.com/{raw}") == f"https://example.com/{expected}"

    @pytest.mark.parametrize(
        "host, expected",
        [
            # Wider than 32 bits: a browser reads the number and then fails the
            # address, so this names no source rather than naming a domain.
            ("99999999999", ""),
            ("9999999999", ""),
            ("0x" + "f" * 20, ""),
            ("0" + "7" * 20, ""),
            ("example.0x" + "F" * 16, ""),
            ("1.2.3." + "9" * 4400, ""),
            # Long enough that CPython refuses the decimal conversion outright.
            ("9" * 5000, ""),
            # The boundary that does fit, and an octal part however many zeros
            # pad it - both measured against ``new URL`` in a real browser.
            ("4294967295", "255.255.255.255"),
            ("0" * 20 + "127", "0.0.0.87"),
        ],
    )
    def test_a_numeric_host_wider_than_an_address_is_refused_not_converted(
        self, host, expected
    ):
        """An untrusted host of digits must not reach ``int`` unbounded.

        CPython refuses to convert a decimal string past
        ``sys.int_info.default_max_str_digits`` (4300), so a host spelled with
        thousands of digits raised ``ValueError`` out of ``safe_url`` - on
        nothing but a search result. The magnitude is what matters anyway: every
        check an IPv4 number reaches rejects a value this big.
        """
        url = f"https://{host}/x"

        assert source_label(url) == expected
        assert bool(safe_url(url)) is bool(expected)

    def test_markdown_punctuation_in_a_url_is_percent_encoded(self):
        """An unencoded ``)`` would truncate the link and leave prose behind it."""
        url = 'https://en.wikipedia.org/wiki/Foo_(bar)?q=a b&t="x"<y>'

        encoded = safe_url(url)

        assert encoded == (
            "https://en.wikipedia.org/wiki/Foo_%28bar%29?q=a%20b&t=%22x%22%3Cy%3E"
        )
        text, citations = resolve(
            f"Claimed.{marker('turn0view0')}",
            {"turn0view0": CitationSource(ref_id="turn0view0", title="T", url=url)},
        )
        assert text == f"Claimed. [en.wikipedia.org]({encoded})"
        assert citations[0]["url"] == encoded

    def test_non_ascii_title_is_preserved_and_collapsed_to_one_line(self):
        source = CitationSource(
            ref_id="turn0view0",
            title="  中文標題 — Café äöü\n\tsecond line  ",
            url="https://example.com/x",
        )

        _, citations = resolve(f"Claimed.{marker('turn0view0')}", {"turn0view0": source})

        assert citations[0]["title"] == "中文標題 — Café äöü second line"

    def test_control_and_private_use_characters_are_stripped_from_a_title(self):
        """A title must not be able to smuggle a marker back into the sidecar."""
        assert clean_title(f"A{START}cite{SEP}turn0view0{END}B​C\x07D") == "A cite turn0view0 B C D"

    @pytest.mark.parametrize("title", [None, 42, b"bytes", {"t": "x"}])
    def test_non_string_titles_collapse_to_empty(self, title):
        assert clean_title(title) == ""

    def test_title_is_bounded(self):
        assert len(clean_title("x" * 5000)) == 200

    def test_label_is_the_domain_lowercased_without_www(self):
        assert source_label("https://WWW.Example.COM/path") == "example.com"
        assert source_label("https://sub.example.co.uk/x") == "sub.example.co.uk"

    def test_label_falls_back_to_the_title_when_there_is_no_host(self):
        assert source_label("not-a-url", "Some Title") == "Some Title"

    def test_markdown_label_punctuation_is_escaped(self):
        """Unreachable through a valid URL (a host has no ``[``), kept as a guard
        because the label falls back to an untrusted title when parsing degrades."""
        from core.citations import _escape_label

        assert _escape_label("a]b[c\\d") == "a\\]b\\[c\\\\d"

    def test_a_title_may_still_spell_an_emoji_or_an_indic_word(self):
        """The sanitizer removes what reorders text, not what joins it.

        A zero-width joiner and a variation selector are how a real title
        spells a family emoji, a Persian word or a text-style symbol. Deleting
        them silently rewrites the title; only the bidi controls, which can
        render a title's own text backwards, have no legitimate use on one
        line.
        """
        assert clean_title("👨\u200d👩\u200d👧 ☎\ufe0f افغانی\u200c ها") == (
            "👨\u200d👩\u200d👧 ☎\ufe0f افغانی\u200c ها"
        )
        assert clean_title("Report\u202egnp.\u202c pdf") == "Report gnp. pdf"

    @pytest.mark.parametrize(
        "label, closer",
        [
            ("ex`ample.com", "`code`"),
            ("a<!--b", "--> and"),
            ("a<?x", "?> and"),
            ("a<!X", "> and"),
            ("a<![CDATA[b", "]]> and"),
        ],
        ids=["code-span", "comment", "instruction", "declaration", "cdata"],
    )
    def test_a_label_cannot_swallow_the_link_it_labels(self, label, closer):
        """Measured against CommonMark: each of these, left raw, takes the link.

        A backtick or an angle bracket in link text does not merely change how
        the text reads. It opens a code span, an HTML comment, a processing
        instruction, a declaration or a CDATA section, which then runs past
        ``](url)`` looking for its closer - and markdown-it renders the whole
        thing as literal text, so the reader is shown raw Markdown with nothing
        to click. Escaping is what keeps the link a link.
        """
        from core.citations import _escape_label

        document = f"Claim. [{_escape_label(label)}](https://s.example/p) then {closer} prose."

        assert MARKDOWN.render(document).count('<a href="https://s.example/p">') == 1

    @pytest.mark.parametrize(
        "label, shown",
        [
            ("a*b*.example", "a<em>b</em>.example"),
            ("a&copy;.example", "a\u00a9.example"),
        ],
        ids=["emphasis", "entity"],
    )
    def test_a_label_reads_as_the_host_it_names(self, label, shown):
        """A label is attribution, so it has to name the host character for
        character - and left raw, each of these names a different one.

        These do not break the link the way a backtick does; they change what
        the reader is told the source IS. ``shown`` records what CommonMark
        makes of the raw label, measured rather than assumed, and the escaped
        label has to render as the host instead. The IM dialects were measured
        too (tests/test_citation_consumers.py): none of them shows the
        backslash.
        """
        from core.citations import _escape_label

        url = "https://s.example/p"
        raw = MARKDOWN.render(f"[{label}]({url})")
        escaped = MARKDOWN.render(f"[{_escape_label(label)}]({url})")

        assert f'<a href="{url}">{shown}</a>' in raw
        assert f'<a href="{url}">{label.replace("&", "&amp;")}</a>' in escaped

    def test_a_label_also_marks_what_only_another_dialect_reads_as_syntax(self):
        """The reader is not always reading CommonMark.

        CommonMark leaves an intraword ``_``, a single ``~`` and a ``|`` alone,
        so the test above cannot show them changing anything. Slack mrkdwn
        reads ``_italic_`` and ``~strike~``, and a ``|`` ends a GFM table cell -
        and a label is untrusted text that lands in all of them. Escaping is
        the one spelling every dialect agrees means "literal"; what each one
        actually shows is measured in tests/test_citation_consumers.py.
        """
        from core.citations import _escape_label

        assert _escape_label("a_b~c|d") == "a\\_b\\~c\\|d"

    @pytest.mark.parametrize(
        "ref_id",
        ["turn0view0 with spaces", "a" * 65, "ref/id", "ref​id", ""],
        ids=["spaces", "too-long", "slash", "zero-width", "empty"],
    )
    def test_malformed_ref_ids_never_reach_the_source_map(self, ref_id):
        text, citations = resolve(
            f"Claimed.{marker(ref_id)}",
            {ref_id: CitationSource(ref_id=ref_id, title="T", url="https://example.com/x")},
        )

        assert "example.com" not in text
        assert citations == []


class TestCodeAndIncompleteMarkers:
    """Where a marker counts as code is a CommonMark question, not a regex one.

    The cases below are the ones local patterns get wrong: fence lengths nest,
    a closing fence may be longer than its opener, an unclosed fence runs to
    the end of the document, a code span may cross lines, and container
    indentation shifts all of it. Both directions are asserted - a literal
    marker keeps every byte, and a marker in prose still resolves even when
    code sits nearby - because over-masking silently drops attribution.
    """

    @pytest.mark.parametrize(
        "template",
        [
            "Inline `{m}` stays literal.",
            "```\n{m}\n```",
            "~~~text\n{m}\n~~~",
            "    {m}",
            "\t{m}",
            "``{m}``",
            "`` ` {m} ``",
            "`line one\n{m}`",
            "````markdown\n```\n{m}\n```\n````",
            "`````\n````\n{m}\n````\n`````",
            "```text\n{m}",
            "~~~\n{m}",
            "- item\n\n  ```\n  {m}\n  ```\n",
            "> ```\n> {m}\n> ```\n",
            "> - a\n>\n>   ```\n>   {m}\n>   ```\n",
            "- item\n\n      {m}\n",
        ],
        ids=[
            "inline",
            "fenced",
            "tilde-fenced",
            "indented",
            "tab-indented",
            "double-backtick-span",
            "double-backtick-span-holding-a-backtick",
            "code-span-across-lines",
            "four-backtick-fence-quoting-three",
            "five-backtick-fence-quoting-four",
            "unclosed-fence-runs-to-end",
            "unclosed-tilde-fence-runs-to-end",
            "fence-inside-a-list-item",
            "fence-inside-a-blockquote",
            "fence-inside-a-blockquoted-list",
            "indented-code-inside-a-list-item",
        ],
    )
    def test_a_marker_inside_a_code_example_is_untouched(self, template):
        raw = template.format(m=marker("turn0view0"))

        text, citations = resolve(raw)

        assert text == raw
        assert citations == []

    @pytest.mark.parametrize(
        "template",
        [
            "- item {m}\n",
            "> quoted {m}\n",
            "```\ncode\n`````\nanswer{m}",
            "````\n```\n````\nanswer{m}",
        ],
        ids=[
            "inside-a-list-item",
            "inside-a-blockquote",
            "after-a-longer-closing-fence",
            "after-a-fence-that-quoted-a-fence",
        ],
    )
    def test_a_marker_in_prose_resolves_even_next_to_code(self, template):
        """Code lexing decides what to skip; it must not swallow real prose."""
        raw = template.format(m=marker("turn0view0"))

        text, citations = resolve(raw)

        assert marker("turn0view0") not in text
        assert "[developers.openai.com]" in text
        assert [c["ref_id"] for c in citations] == ["turn0view0"]

    def test_prose_around_a_code_example_still_resolves(self):
        raw = f"See{marker('turn0view0')}\n\n```\n{marker('turn0view0')}\n```\n\nAnd{marker('turn0view1')}"

        text, citations = resolve(raw)

        assert f"```\n{marker('turn0view0')}\n```" in text
        assert text.count("[developers.openai.com]") == 1
        assert [c["label"] for c in citations] == ["developers.openai.com", "example.com"]

    @pytest.mark.parametrize(
        "raw",
        [
            f"split {START}cite{SEP}turn0view0",
            f"split cite{SEP}turn0view0{END}",
            f"{START}visualize{SEP}" '{"a":1}' f"{END} keep",
        ],
        ids=["no-terminator", "no-opener", "sibling-marker"],
    )
    def test_an_incomplete_or_foreign_marker_is_left_alone(self, raw):
        assert resolve(raw) == (raw, [])

    def test_a_restarted_marker_resolves_the_complete_half_and_keeps_the_truncated_one(self):
        """Chunked delivery can strip a terminator; the well-formed half still counts."""
        truncated = f"{START}cite{SEP}turn0view0"

        text, citations = resolve(f"nested {truncated}{marker('turn0view1')}")

        assert text == (
            f"nested {truncated} [example.com](https://example.com/citation-probe-source)"
        )
        assert [c["ref_id"] for c in citations] == ["turn0view1"]


class TestDetectionAndSpacing:
    @pytest.mark.parametrize(
        "text, expected",
        [
            (None, False),
            ("", False),
            ("plain text", False),
            (f"{START}cite{SEP}turn0view0", False),
            (f"{START}visualize{SEP}x{END}", False),
            (f"has{marker('turn0view0')}", True),
        ],
    )
    def test_has_citation_markers(self, text, expected):
        assert has_citation_markers(text) is expected

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("word{m}", "word [example.com](https://example.com/citation-probe-source)"),
            ("word {m}", "word [example.com](https://example.com/citation-probe-source)"),
            ("{m}", "[example.com](https://example.com/citation-probe-source)"),
            ("word.\n{m}", "word.\n[example.com](https://example.com/citation-probe-source)"),
        ],
        ids=["abutting", "already-spaced", "at-start", "after-newline"],
    )
    def test_one_separating_space_is_added_only_when_needed(self, raw, expected):
        text, _ = resolve(raw.format(m=marker("turn0view1")))

        assert text == expected

    @pytest.mark.parametrize("text", [None, "", "no markers here"])
    def test_text_without_markers_is_returned_unchanged_with_no_sidecar(self, text):
        assert resolve(text) == (text, [])


class TestHiddenBlocks:
    """A citation inside a ``<silent>`` block belongs to no delivered message.

    The block is removed downstream of this rewrite, so numbering a marker
    inside one attributes a source the reader never sees: the sidecar opens at
    index 2, the badge it describes has no link, and the visible citation is
    misnumbered. Hidden markers are therefore skipped exactly the way code is -
    left literal, so they leave with the block that hid them.
    """

    def test_a_hidden_marker_is_neither_numbered_nor_rewritten(self):
        raw = (
            f"<silent>internal note{marker('turn0view0')}</silent>"
            f"Visible.{marker('turn0view1')}"
        )

        text, citations = resolve(raw)

        assert marker("turn0view0") in text
        assert text.endswith("Visible. [example.com](https://example.com/citation-probe-source)")
        assert [(c["index"], c["ref_id"]) for c in citations] == [(1, "turn0view1")]

    def test_stripping_the_block_leaves_exactly_the_visible_citation(self):
        raw = (
            f"<silent>hidden{marker('turn0view0')}</silent>"
            f"Visible.{marker('turn0view1')}"
        )

        text, _ = resolve(raw)

        assert strip_silent_blocks(text) == (
            "Visible. [example.com](https://example.com/citation-probe-source)"
        )

    def test_an_entirely_hidden_message_carries_no_sidecar(self):
        raw = f"<silent>only this{marker('turn0view0')}</silent>"

        text, citations = resolve(raw)

        assert text == raw
        assert citations == []

    def test_a_hidden_block_inside_a_code_example_hides_nothing(self):
        """``<silent>`` shown as code is not a control, so the prose marker resolves."""
        raw = f"`<silent>`\nVisible.{marker('turn0view0')}"

        text, citations = resolve(raw)

        assert "[developers.openai.com]" in text
        assert [c["ref_id"] for c in citations] == ["turn0view0"]


class TestRequestedRefs:
    """What a message asks for, which is what a backend has to go looking for.

    A backend reads its recorded history to fill the gaps in a message's
    attribution, so it needs the refs that message actually requests - not every
    ref it ever saw. The same eligibility rules apply as to the rewrite: code and
    hidden blocks ask for nothing, and a malformed ref is not a request.
    """

    def test_the_requested_refs_are_reported_in_first_appearance_order(self):
        text = f"A{marker('turn0view1')} B{marker('turn0view0', 'turn0view1')}"

        assert citation_ref_ids(text) == ["turn0view1", "turn0view0"]

    @pytest.mark.parametrize(
        "text",
        [None, "", "plain text", f"{START}cite{SEP}turn0view0", f"{START}visualize{SEP}x{END}"],
        ids=["none", "empty", "no-marker", "unterminated", "sibling-marker"],
    )
    def test_text_that_asks_for_nothing_reports_nothing(self, text):
        assert citation_ref_ids(text) == []

    def test_a_marker_in_code_asks_for_nothing(self):
        assert citation_ref_ids(f"```\n{marker('turn0view0')}\n```") == []

    def test_a_marker_in_a_hidden_block_asks_for_nothing(self):
        assert citation_ref_ids(f"<silent>{marker('turn0view0')}</silent>") == []

    def test_a_malformed_ref_is_not_a_request(self):
        assert citation_ref_ids(f"A{marker('has space', 'turn0view0', 'x' * 65)}") == [
            "turn0view0"
        ]


class TestUnresolvedRefs:
    """Which requested refs are still waiting for a source, and which never will be.

    A backend holds a message while a ref it names may still arrive. A ref whose
    source is already present but unusable is not waiting for anything - holding
    it would delay the message forever - so it is reported as resolved-as-far-as-
    it-goes and degrades to the visible label instead.
    """

    def test_a_ref_with_no_source_is_unresolved(self):
        assert unresolved_refs(["turn0view0", "turn9view9"], SOURCES) == ["turn9view9"]

    def test_a_fully_sourced_message_is_not_waiting(self):
        assert unresolved_refs(["turn0view0", "turn0view1"], SOURCES) == []

    @pytest.mark.parametrize(
        "url",
        ["javascript:alert(1)", "", "https:///path", "not-a-url"],
        ids=["unsafe-scheme", "empty", "hostless", "unparseable-authority"],
    )
    def test_a_source_that_can_never_be_linked_is_not_waited_for(self, url):
        sources = {"turn0view0": CitationSource(ref_id="turn0view0", title="T", url=url)}

        assert unresolved_refs(["turn0view0"], sources) == []

    def test_nothing_requested_means_nothing_unresolved(self):
        assert unresolved_refs([], SOURCES) == []


class TestLinkProvenance:
    """Which links in the delivered text each citation actually wrote.

    A badge is an attribution claim, so it belongs to a link this module wrote
    and not to any link the answer's own prose happens to point at the same
    page. Recognizing one used to mean matching a rendered label back to a
    sidecar entry, which cannot tell those two apart - and got the claim wrong
    in the direction that matters, since the prose link is the one nobody
    vouched for.

    So the sidecar carries the answer instead, as one exact string: the
    complete link this module wrote, character for character, plus which of
    that spelling's literal occurrences in the delivered text are its own.

    Counting a literal substring is the one measurement two Markdown
    implementations cannot disagree about. Counting parsed links by destination
    did disagree - a GFM footnote definition holding ``[p](url)`` is a link to
    one and part of a definition to the other - and the real citation lost its
    badge over the difference. So everything in the delivered text counts here,
    code examples and footnotes included, and the consumer counts the same
    characters in the same text.
    """

    GUIDE_URL = "https://developers.openai.com/api/docs/guides/tools-web-search"
    GUIDE_LINK = f"[developers.openai.com]({GUIDE_URL})"

    def test_a_citation_records_the_link_it_wrote(self):
        _, citations = resolve(f"Documented.{marker('turn0view0')}")

        assert citations[0]["spelling"] == self.GUIDE_LINK
        assert citations[0]["occurrences"] == [1]
        assert citations[0]["occurrence_total"] == 1

    def test_prose_worded_its_own_way_is_a_different_string_entirely(self):
        """Same page, different characters - so it is not this citation's link.

        The old contract counted by destination and had to number this one to
        stay in step with the consumer. Counting the exact spelling makes it
        simply absent: nothing in the answer's own wording can shift a
        citation's position, and the consumer reaches the same conclusion from
        the same text.
        """
        _, citations = resolve(
            f"See [the guide]({self.GUIDE_URL}) first.{marker('turn0view0')}"
        )

        assert citations[0]["occurrences"] == [1]
        assert citations[0]["occurrence_total"] == 1

    def test_prose_that_spells_the_link_identically_is_counted_but_not_claimed(self):
        """Character for character the same link - so the reader cannot tell
        them apart either, and neither side pretends to. It is counted, which
        keeps both totals equal, and left unclaimed, which keeps the badge on
        the one this module wrote."""
        _, citations = resolve(
            f"See {self.GUIDE_LINK} first.{marker('turn0view0')}"
        )

        assert citations[0]["occurrences"] == [2]
        assert citations[0]["occurrence_total"] == 2

    def test_an_identical_prose_link_after_the_marker_shifts_nothing_before_it(self):
        _, citations = resolve(
            f"Documented.{marker('turn0view0')} Also {self.GUIDE_LINK}."
        )

        assert citations[0]["occurrences"] == [1]
        assert citations[0]["occurrence_total"] == 2

    def test_one_source_cited_twice_claims_both_of_its_links(self):
        _, citations = resolve(f"One.{marker('turn0view0')} Two.{marker('turn1view0')}")

        assert citations[0]["occurrences"] == [1, 2]
        assert citations[0]["occurrence_total"] == 2

    def test_ordinals_are_counted_per_spelling(self):
        """A citation to another page sits between these two and moves neither."""
        _, citations = resolve(
            f"A.{marker('turn0view0')} B.{marker('turn0view1')} C.{marker('turn1view0')}"
        )

        assert [c["occurrences"] for c in citations] == [[1, 2], [1]]
        assert [c["occurrence_total"] for c in citations] == [2, 1]

    @pytest.mark.parametrize(
        "decoy",
        [
            "`{link}`",
            "```\n{link}\n```",
            "[^f]: {link}",
            "| {link} |",
            "![alt]({url})\n\n{link}",
            "<div>{link}</div>",
        ],
        ids=["code-span", "code-fence", "footnote", "table-cell", "after-image", "html"],
    )
    def test_every_literal_occurrence_counts_wherever_it_sits(self, decoy):
        """The total has to mean the same thing on both sides of the boundary.

        A parser decides whether each of these is a link; a literal scan does
        not have to, and that is the point - the consumer scans the same
        characters and reaches the same numbers without either side agreeing
        about footnotes, tables or HTML blocks. The citation still takes the
        occurrence it wrote, which is the last one here.
        """
        _, citations = resolve(
            decoy.format(link=self.GUIDE_LINK, url=self.GUIDE_URL)
            + f"\n\nShown.{marker('turn0view0')}"
        )

        assert citations[0]["occurrence_total"] == 2
        assert citations[0]["occurrences"] == [2]

    @pytest.mark.parametrize(
        "decoy",
        [
            "![developers.openai.com]({url})",
            "<{url}>",
            "[dup][k]",
            "[developers.openai.com]({url} \"t\")",
        ],
        ids=["image", "autolink", "reference-link", "titled-link"],
    )
    def test_a_link_spelled_any_other_way_is_not_this_citation(self, decoy):
        """Same destination, different characters. An image is the one to watch:
        ``![label](url)`` does contain ``[label](url)``, so it IS one literal
        occurrence - and the consumer, scanning the same text, counts it too."""
        # An image spells the citation's own link with one `!` in front of it.
        total = 2 if decoy.startswith("!") else 1
        _, citations = resolve(
            f"{decoy.format(url=self.GUIDE_URL)} Shown.{marker('turn0view0')}"
            f"\n\n[k]: {self.GUIDE_URL}"
        )

        assert citations[0]["occurrence_total"] == total
        assert citations[0]["occurrences"] == [total]

    def test_a_hidden_citation_is_not_counted_against_the_visible_one(self):
        """Stripping the block must not leave the sidecar describing text that left with it."""
        text, citations = resolve(
            f"<silent>Hidden.{marker('turn0view0')}</silent>Shown.{marker('turn1view0')}"
        )
        delivered = strip_silent_blocks(text)

        assert delivered.count(self.GUIDE_LINK) == 1
        assert citations[0]["occurrences"] == [1]
        assert citations[0]["occurrence_total"] == 1

    def test_a_hidden_copy_of_the_link_is_not_counted_either(self):
        """The one delivery transform measured to move an occurrence: the block
        is removed, so counting its copy would put every later ordinal one
        ahead of what the consumer can see."""
        text, citations = resolve(
            f"<silent>{self.GUIDE_LINK}</silent>Shown.{marker('turn0view0')}"
        )
        delivered = strip_silent_blocks(text)

        assert delivered.count(self.GUIDE_LINK) == 1
        assert citations[0]["occurrences"] == [1]
        assert citations[0]["occurrence_total"] == 1


class TestUrlIdentityAcrossTheBoundary:
    """One URL table, asserted on both sides of the backend/renderer boundary.

    The sidecar is matched against the ``href`` the Markdown renderer produced,
    so the two have to agree byte for byte. The renderer normalizes its link
    destinations (percent-encoding, escape resolution, surrogate repair) and the
    backend cannot see that happen, so every case here is also rendered by
    ``ui/src/components/ui/markdown.test.tsx`` from this same fixture. A ``url``
    of ``null`` means the raw value must be rejected rather than canonicalized.

    The fixture's ``authority`` half is the same table for the userinfo, host
    and port, and it is read straight into the same cases: an authority is one
    thing a browser decides all at once, so a new shape belongs in that table
    rather than in a branch of its own.
    """

    FIXTURE = json.loads(
        (Path(__file__).resolve().parent / "fixtures/citation_url_identity.json").read_text(
            encoding="utf-8"
        )
    )
    CASES = [*FIXTURE["cases"], *FIXTURE["authority"]]

    @pytest.mark.parametrize("case", CASES, ids=[c["why"] for c in CASES])
    def test_the_backend_produces_the_url_the_renderer_will_keep(self, case):
        assert safe_url(case["raw"]) == (case["url"] or "")

    @pytest.mark.parametrize(
        "case", [c for c in CASES if c["url"]], ids=[c["why"] for c in CASES if c["url"]]
    )
    def test_the_label_attributes_the_host_a_browser_would_reach(self, case):
        assert source_label(case["url"]) == case["label"]

    @pytest.mark.parametrize(
        "case", [c for c in CASES if c["url"]], ids=[c["why"] for c in CASES if c["url"]]
    )
    def test_canonicalizing_a_canonical_url_changes_nothing(self, case):
        """The rewrite runs once per message; it must be a fixed point regardless."""
        assert safe_url(case["url"]) == case["url"]

    def test_a_rewritten_message_carries_the_canonical_url_in_both_halves(self):
        raw = "https://example.com/中文"
        sources = {"turn0view0": CitationSource(ref_id="turn0view0", title="T", url=raw)}

        text, citations = resolve(f"Cited.{marker('turn0view0')}", sources)

        assert citations[0]["url"] == "https://example.com/%E4%B8%AD%E6%96%87"
        assert f"({citations[0]['url']})" in text
