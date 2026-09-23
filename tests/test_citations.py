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

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.citations import (
    _TOKEN_CLOSE as TOKEN_CLOSE,
)
from core.citations import (
    _TOKEN_OPEN as TOKEN_OPEN,
)
from core.citations import (
    CitationSource,
    body_digest,
    citation_ref_ids,
    clean_title,
    finalize_citations,
    has_citation_markers,
    materialize_citations,
    register_citations,
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


def utf16_slice(text: str, start: int, end: int) -> str:
    """The substring a span names - sliced the way the consumer slices it.

    Spans are UTF-16 code units because that is what a browser counts;
    ``text[start:end]`` would silently disagree the moment an astral character
    appears above the span.
    """
    units = text.encode("utf-16-le", "surrogatepass")
    return units[start * 2 : end * 2].decode("utf-16-le", "surrogatepass")


def charcodeat_bytes(text: str) -> bytes:
    """The bytes a ``charCodeAt`` loop in the browser would hash.

    Written out unit by unit rather than handed to a codec, so the fixture is
    checked against what JavaScript does and not against the same Python call
    the implementation makes.
    """
    units: list[int] = []
    for char in text:
        code = ord(char)
        if code > 0xFFFF:
            code -= 0x10000
            units.append(0xD800 + (code >> 10))
            units.append(0xDC00 + (code & 0x3FF))
        else:
            units.append(code)
    return b"".join(unit.to_bytes(2, "little") for unit in units)


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
                # Where the link sits in THIS body, and the digest that binds
                # the two together. See TestBodyBinding.
                "spans": [[29, len(text)]],
                "body_sha256": body_digest(text),
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

    def test_markdown_punctuation_is_protected_in_the_link_and_not_in_the_url(self):
        """A parenthesis is part of the address, and only Markdown needs it guarded.

        Percent-encoding it in the URL protected the link and moved the page:
        ``Foo_%28bar%29`` is not the path the source gave. The URL keeps it, and
        the link the answer carries escapes it, which every reader resolves back.
        """
        url = 'https://en.wikipedia.org/wiki/Foo_(bar)?q=a b&t="x"<y>'

        canonical = safe_url(url)

        assert canonical == (
            "https://en.wikipedia.org/wiki/Foo_(bar)?q=a%20b&t=%22x%22%3Cy%3E"
        )
        text, citations = resolve(
            f"Claimed.{marker('turn0view0')}",
            {"turn0view0": CitationSource(ref_id="turn0view0", title="T", url=url)},
        )
        assert text == (
            "Claimed. [en.wikipedia.org]"
            "(https://en.wikipedia.org/wiki/Foo_\\(bar\\)?q=a%20b&t=%22x%22%3Cy%3E)"
        )
        assert citations[0]["url"] == canonical

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)"),
            ("https://example.com/a_(b", "https://example.com/a_(b"),
            ("https://example.com/a)b", "https://example.com/a)b"),
            ("https://example.com/a%28b%29", "https://example.com/a%28b%29"),
            ("https://example.com/a&#40;b&#x29;", "https://example.com/a(b)"),
            ("https://[::1]:8443/x(y?q=(1)", "https://[::1]:8443/x(y?q=(1)"),
        ],
        ids=["balanced", "unbalanced-open", "unbalanced-close", "already-encoded",
             "character-reference", "ipv6"],
    )
    def test_a_parenthesis_reaches_the_reader_as_the_source_wrote_it(self, raw, expected):
        """The sidecar, the link and what a Markdown reader resolves all agree."""
        assert safe_url(raw) == expected

        text, citations = resolve(
            f"Claimed.{marker('turn0view0')}",
            {"turn0view0": CitationSource(ref_id="turn0view0", title="T", url=raw)},
        )

        assert citations[0]["url"] == expected
        tokens = MARKDOWN.parseInline(text)[0].children
        hrefs = [token.attrGet("href") for token in tokens if token.type == "link_open"]
        assert hrefs == [MARKDOWN.normalizeLink(expected)]
        assert text.endswith(")")

    @pytest.mark.parametrize(
        "url",
        [
            "https://trusted.example\\@attacker.example/x",
            "https://example.com/a\\b",
            "https://example.com/p?q=a\\b",
            "https://example.com/a&#92;b",
            "https://example.com/a&bsol;b",
        ],
        ids=["authority", "path", "query", "numeric-reference", "named-reference"],
    )
    def test_a_backslash_left_in_the_address_names_no_source(self, url):
        """A browser reads ``\\`` as ``/`` in an http(s) authority and path, and
        as data in a query, so no one spelling of it names the page for every
        reader - ``trusted.example\\@attacker.example`` opens trusted.example
        in a browser and was attributed to attacker.example. It is refused."""
        assert safe_url(url) == ""

        text, citations = resolve(
            f"Claimed.{marker('turn0view0')}",
            {"turn0view0": CitationSource(ref_id="turn0view0", title="T", url=url)},
        )
        assert text == f"Claimed. {UNRESOLVED}"
        assert citations == []

    def test_an_encoded_backslash_is_an_ordinary_character_of_the_address(self):
        """``%5C`` is data to every reader, so it is kept exactly as written."""
        url = "https://example.com/a%5Cb?q=c%5Cd"

        assert safe_url(url) == url
        _, citations = resolve(
            f"Claimed.{marker('turn0view0')}",
            {"turn0view0": CitationSource(ref_id="turn0view0", title="T", url=url)},
        )
        assert citations[0]["url"] == url

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


class TestRegisteredIdentity:
    """Which text in a delivered message is allowed to be a citation at all.

    A marker means what it says exactly once: in the text the backend produced.
    Everything downstream rewrites that text - a ``<silent>`` block leaves, a
    ``file://`` link is stripped to its label, images are appended - and a
    rewrite can splice two ordinary halves into characters that read as a
    marker. So the markers are registered against the native text and travel as
    opaque per-occurrence tokens; the stage that finally writes the links reads
    only those tokens and never the grammar again.
    """

    GUIDE_URL = "https://developers.openai.com/api/docs/guides/tools-web-search"
    GUIDE_LINK = f"[developers.openai.com]({GUIDE_URL})"

    def test_a_marker_a_later_transform_splices_together_is_not_a_citation(self):
        """The counterexample that decides where registration has to happen.

        Only the first of these is a citation. Strip the hidden block - which
        every delivered copy does - and the second half is character for
        character a legal marker too. A stage that read the grammar here would
        attribute a page to text the model never cited.
        """
        native = (
            f"Actual.{marker('turn0view0')}\n\n"
            f"Synthetic. {START}ci<silent>private</silent>te{SEP}turn0view0{END}"
        )
        registered, bundle = register_citations(
            native, SOURCES, unresolved_label=UNRESOLVED
        )
        delivered = strip_silent_blocks(registered)
        body, citations = finalize_citations(delivered, bundle)

        assert body.count(self.GUIDE_LINK) == 1
        assert body.endswith(f"Synthetic. {marker('turn0view0')}")
        assert len(citations) == 1
        assert citations[0]["spans"] == [
            [body.index(self.GUIDE_LINK), body.index(self.GUIDE_LINK) + len(self.GUIDE_LINK)]
        ]

    def test_a_copy_of_a_registered_token_still_speaks_for_its_citation(self):
        """A transform that duplicates text duplicates the citation with it -
        the copy is the same registered occurrence, not a new claim."""
        registered, bundle = register_citations(
            f"Cited.{marker('turn0view0')}", SOURCES, unresolved_label=UNRESOLVED
        )
        body, citations = finalize_citations(f"{registered}\n\n{registered}", bundle)

        assert body.count(self.GUIDE_LINK) == 2
        assert len(citations) == 1
        assert [utf16_slice(body, *span) for span in citations[0]["spans"]] == [
            self.GUIDE_LINK,
            self.GUIDE_LINK,
        ]

    def test_token_shaped_text_this_bundle_never_minted_is_left_alone(self):
        """An identity is a nonce this call handed out, not a shape."""
        registered, bundle = register_citations(
            f"Cited.{marker('turn0view0')}", SOURCES, unresolved_label=UNRESOLVED
        )
        forged = f"{TOKEN_OPEN}{'0' * 32}{TOKEN_CLOSE}"
        body, citations = finalize_citations(f"{forged} {registered}", bundle)

        assert body.startswith(forged)
        assert len(citations) == 1
        assert [utf16_slice(body, *span) for span in citations[0]["spans"]] == [
            self.GUIDE_LINK
        ]

    def test_a_token_a_transform_deleted_describes_nothing(self):
        registered, bundle = register_citations(
            f"Cited.{marker('turn0view0')}", SOURCES, unresolved_label=UNRESOLVED
        )
        body, citations = finalize_citations("Nothing left.", bundle)

        assert TOKEN_OPEN in registered
        assert body == "Nothing left."
        assert citations == []

    @pytest.mark.parametrize(
        "template",
        ["`{token}`", "```\n{token}\n```", "<silent>{token}</silent>"],
        ids=["code-span", "code-fence", "hidden"],
    )
    def test_a_token_a_transform_moved_into_code_shows_the_model_s_own_text(
        self, template
    ):
        """An internal token is the one thing a reader may never be shown, and
        a code example is not an attribution either - so the marker the model
        wrote comes back verbatim and no badge is claimed for it."""
        registered, bundle = register_citations(
            f"Cited.{marker('turn0view0')}", SOURCES, unresolved_label=UNRESOLVED
        )
        token = registered[len("Cited.") :]
        body, citations = finalize_citations(
            template.format(token=token), bundle
        )

        assert body == template.format(token=marker("turn0view0"))
        assert citations == []


class TestWhereACitationCanBeShown:
    """Which part of a link may become a citation, and which is only its wiring.

    A citation is a link, and CommonMark has no link inside a link: written into
    a label it takes the enclosing unit down with it, and the reader is shown
    the brackets instead of the page the answer meant to point at. Written into
    a destination, a title, a reference identifier or an autolink's address it
    is not shown to anyone at all - that text is what makes the unit reach
    somewhere - and splicing a link through it sends the unit to an address
    nobody wrote.

    So the parser decides: a marker standing where a reader is shown something
    is a citation, and its source is written immediately after the outermost
    unit that encloses it; a marker standing in a slot only the parser reads
    stays the characters the model typed, mints no token, and claims nothing.
    """

    GUIDE_LINK = f"[developers.openai.com]({GUIDE.url})"

    def written(self, native: str) -> tuple[str, list[dict]]:
        """What delivery finally writes for *native*, and what it claims."""
        registered, bundle = register_citations(
            native, SOURCES, unresolved_label=UNRESOLVED
        )
        return finalize_citations(registered, bundle)

    @pytest.mark.parametrize(
        ("native", "expected"),
        [
            (
                f"See [the docs {marker('turn0view0')}](https://openai.com/docs) for details.",
                "See [the docs ](https://openai.com/docs) {link} for details.",
            ),
            (
                f"[{marker('turn0view0')} a](https://u.example/1)",
                "[ a](https://u.example/1) {link}",
            ),
            (
                f"[a {marker('turn0view0')} b](https://u.example/1)",
                "[a  b](https://u.example/1) {link}",
            ),
            (
                f"![a diagram {marker('turn0view0')}](https://i.example/p.png)",
                "![a diagram ](https://i.example/p.png) {link}",
            ),
            (
                f"[![alt {marker('turn0view0')}](https://i.example/p.png)](https://out.example/page)",
                "[![alt ](https://i.example/p.png)](https://out.example/page) {link}",
            ),
            (
                f"[the docs {marker('turn0view0')}][ref]\n\n[ref]: https://r.example/page",
                "[the docs ][ref] {link}\n\n[ref]: https://r.example/page",
            ),
            (
                f"\u65e5\u672c\u8a9e [\u30e9\u30d9\u30eb {marker('turn0view0')}](https://u.example/1)",
                "\u65e5\u672c\u8a9e [\u30e9\u30d9\u30eb ](https://u.example/1) {link}",
            ),
        ],
        ids=[
            "label-middle",
            "label-start",
            "label-inner",
            "image-alt",
            "image-inside-a-link",
            "explicit-reference-label",
            "non-ascii-before-it",
        ],
    )
    def test_a_source_is_written_after_the_unit_whose_label_asked_for_it(
        self, native, expected
    ):
        """The destination the answer wrote survives, and so does its wording.

        An image inside a link is the case that makes "the outermost unit" the
        rule rather than "the unit it was in": place the source after the image
        and it lands in the link's label, which is the defect this describes.
        """
        body, citations = self.written(native)

        assert body == expected.format(link=self.GUIDE_LINK)
        assert [utf16_slice(body, *span) for span in citations[0]["spans"]] == [
            self.GUIDE_LINK
        ]

    @pytest.mark.parametrize(
        "native",
        [
            f"[a](https://example.com/{marker('turn0view0')}path)",
            f'[b](https://example.com/y "T {marker("turn0view0")}")',
            f"[c](<https://example.com/z{marker('turn0view0')}>)",
            f"<https://example.com/auto{marker('turn0view0')}>",
            f"![plain](https://i.example/{marker('turn0view0')}q.png)",
            f"[the docs][{marker('turn0view0')}]\n\n[{marker('turn0view0')}]: https://r.example/p",
            f"[dual {marker('turn0view0')}]\n\n[dual {marker('turn0view0')}]: https://r.example/d",
            f"[dual {marker('turn0view0')}][]\n\n[dual {marker('turn0view0')}]: https://r.example/d",
            f"[docs {marker('turn0view0')}]\n\n[docs]: https://r.example/d",
            f"[docs {marker('turn0view0')}][]\n\n[docs]: https://r.example/d",
            f"![docs {marker('turn0view0')}]\n\n[docs]: https://i.example/d.png",
            f"![docs {marker('turn0view0')}][]\n\n[docs]: https://i.example/d.png",
        ],
        ids=[
            "destination",
            "title",
            "angle-destination",
            "angle-autolink",
            "image-source",
            "reference-identifier-and-definition",
            "shortcut-reference",
            "collapsed-reference",
            "shortcut-reference-the-marker-unresolves",
            "collapsed-reference-the-marker-unresolves",
            "shortcut-image-the-marker-unresolves",
            "collapsed-image-the-marker-unresolves",
        ],
    )
    def test_a_slot_only_the_parser_reads_keeps_the_model_s_own_text(self, native):
        """Nothing is minted, nothing is written, and nothing is waited for.

        A shortcut or collapsed reference is the dual-use case: those brackets
        are both the words a reader sees and the identifier that finds the
        address, so editing them would leave a link pointing at nothing.

        A marker the definition does not repeat is the same run all the same.
        CommonMark cannot match ``[docs <marker>]`` against ``[docs]`` and so
        reads the brackets as prose, but the run is still what names that
        definition - and a citation written into it costs the reader the link
        the definition was about to give them, image or link alike.
        """
        body, citations = self.written(native)

        assert body == native
        assert citations == []
        # The same decision on both sides of the boundary: a marker that cannot
        # become a link must not hold the message waiting for a source either.
        assert citation_ref_ids(native) == []
        assert unresolved_refs(citation_ref_ids(native), {}) == []

    def test_two_markers_in_two_labels_are_two_sources_in_reading_order(self):
        native = (
            f"[a {marker('turn0view0')}](https://u.example/1)"
            f" and [b {marker('turn0view0')}](https://u.example/2)"
        )
        body, citations = self.written(native)

        assert body == (
            f"[a ](https://u.example/1) {self.GUIDE_LINK}"
            f" and [b ](https://u.example/2) {self.GUIDE_LINK}"
        )
        # One source cited twice is one row and two spans - the numbering counts
        # sources a reader can see, not markers the model wrote.
        assert len(citations) == 1
        assert [utf16_slice(body, *span) for span in citations[0]["spans"]] == [
            self.GUIDE_LINK,
            self.GUIDE_LINK,
        ]

    @pytest.mark.parametrize(
        ("moved", "expected"),
        [
            ("[lab {token}](https://out.example/page)", "[lab ](https://out.example/page) {link}"),
            ("[a](https://example.com/{token}path)", "[a](https://example.com/{marker}path)"),
            ('[b](https://example.com/y "T {token}")', '[b](https://example.com/y "T {marker}")'),
        ],
        ids=["into-a-label", "into-a-destination", "into-a-title"],
    )
    def test_a_token_a_transform_moved_is_judged_where_it_ended_up(
        self, moved, expected
    ):
        """Delivery closes gaps, and a token can land somewhere the model never
        put it. Writing a link into a destination on the strength of where the
        marker started is how an answer acquires an address nobody wrote."""
        registered, bundle = register_citations(
            f"Cited.{marker('turn0view0')}", SOURCES, unresolved_label=UNRESOLVED
        )
        token = registered[len("Cited.") :]
        body, citations = finalize_citations(moved.format(token=token), bundle)

        assert body == expected.format(
            link=self.GUIDE_LINK, marker=marker("turn0view0")
        )
        assert bool(citations) is ("{link}" in expected)

    def test_a_field_that_is_not_markdown_keeps_plain_attribution_in_place(self):
        """An attachment's title and a quick-reply button are not Markdown a
        reader parses, so there is no unit to place a source after and no link
        syntax to write: the source's name stands where the marker stood."""
        registered, bundle = register_citations(
            f"report {marker('turn0view0')}", SOURCES, unresolved_label=UNRESOLVED
        )
        token = registered[len("report ") :]

        assert (
            materialize_citations(registered, bundle, as_markdown=False)
            == "report developers.openai.com"
        )
        assert (
            materialize_citations(f"OK {token}", bundle, as_markdown=False)
            == "OK developers.openai.com"
        )
        assert TOKEN_OPEN not in materialize_citations(
            registered, bundle, as_markdown=False
        )


class TestBodyBinding:
    """Which body the sidecar describes, and where in it each citation sits.

    A badge is an attribution claim, so it belongs to a link this module wrote
    and not to any link the answer's own prose happens to point at the same
    page. The rows therefore carry the exact ``spans`` their links occupy -
    UTF-16 code units, half-open, the coordinates the browser counts in.

    A span alone is not an identity. Two ordinary links can be spelled the
    same, and deleting a paragraph above one moves another into the
    coordinates it vacated: measured with react-markdown, ``L + "\\n\\n" + L``
    puts links at ``[0, 36]`` and ``[38, 74]``, so dropping the first
    paragraph leaves an ordinary link sitting exactly where a citation was
    measured. So every row also carries the digest of the body it was measured
    in, and a consumer that cannot reproduce that digest paints no badge.
    """

    GUIDE_URL = "https://developers.openai.com/api/docs/guides/tools-web-search"
    GUIDE_LINK = f"[developers.openai.com]({GUIDE_URL})"

    def test_a_citation_spans_the_link_it_wrote(self):
        text, citations = resolve(f"Documented.{marker('turn0view0')}")

        assert [utf16_slice(text, *span) for span in citations[0]["spans"]] == [
            self.GUIDE_LINK
        ]
        assert citations[0]["body_sha256"] == body_digest(text)

    def test_the_lead_space_and_the_fallback_label_belong_to_no_citation(self):
        """The span is the link, not the punctuation around it, and text that
        stands in for a source nobody could resolve is nobody's link."""
        text, citations = resolve(
            f"Both.{marker('turn0view0', 'turn9view9')}"
        )

        assert text == f"Both. {self.GUIDE_LINK} {UNRESOLVED}"
        assert [utf16_slice(text, *span) for span in citations[0]["spans"]] == [
            self.GUIDE_LINK
        ]

    def test_prose_that_spells_the_same_link_is_not_the_one_described(self):
        """Character for character the same link, so the reader cannot tell
        them apart either - and neither side pretends to. The badge stays on
        the one this module wrote."""
        text, citations = resolve(f"See {self.GUIDE_LINK} first.{marker('turn0view0')}")

        assert text.count(self.GUIDE_LINK) == 2
        assert citations[0]["spans"] == [
            [text.rindex(self.GUIDE_LINK), len(text)]
        ]

    def test_one_source_cited_twice_spans_both_of_its_links(self):
        text, citations = resolve(f"One.{marker('turn0view0')} Two.{marker('turn1view0')}")

        assert len(citations) == 1
        assert [utf16_slice(text, *span) for span in citations[0]["spans"]] == [
            self.GUIDE_LINK,
            self.GUIDE_LINK,
        ]

    def test_a_citation_to_another_page_moves_neither_of_its_neighbours(self):
        text, citations = resolve(
            f"A.{marker('turn0view0')} B.{marker('turn0view1')} C.{marker('turn1view0')}"
        )

        assert [
            [utf16_slice(text, *span) for span in c["spans"]] for c in citations
        ] == [
            [self.GUIDE_LINK, self.GUIDE_LINK],
            [f"[example.com]({PROBE.url})"],
        ]

    def test_spans_are_counted_in_utf16_code_units(self):
        """What the browser counts, not what Python iterates: an astral emoji
        is one character here and two code units there, and the consumer reads
        the second number."""
        text, citations = resolve(f"🙂🙂 Documented.{marker('turn0view0')}")
        start, end = citations[0]["spans"][0]

        assert start == text.index(self.GUIDE_LINK) + 2
        assert utf16_slice(text, start, end) == self.GUIDE_LINK

    def test_every_row_of_one_body_carries_that_body_s_digest(self):
        text, citations = resolve(
            f"A.{marker('turn0view0')} B.{marker('turn0view1')}"
        )

        assert len(citations) == 2
        assert {c["body_sha256"] for c in citations} == {body_digest(text)}

    def test_a_transform_between_registration_and_delivery_moves_the_spans(self):
        """The reason the two halves exist. The hidden block leaves before the
        links are written, so the spans are measured in the body that ships."""
        registered, bundle = register_citations(
            f"<silent>Private.</silent>Shown.{marker('turn0view0')}",
            SOURCES,
            unresolved_label=UNRESOLVED,
        )
        body, citations = finalize_citations(strip_silent_blocks(registered), bundle)

        assert body == f"Shown. {self.GUIDE_LINK}"
        assert [utf16_slice(body, *span) for span in citations[0]["spans"]] == [
            self.GUIDE_LINK
        ]
        assert citations[0]["body_sha256"] == body_digest(body)

    def test_a_link_that_outlives_the_citation_beside_it_is_described_by_nobody(self):
        """The impersonation counterexample, in the one place it can be stopped.

        Delete the paragraph a citation was written into and the answer's own
        prose link slides into the coordinates it held. Nothing downstream can
        tell the difference by looking; the sidecar simply never describes it,
        because the token that spoke for the citation left with the paragraph.
        """
        registered, bundle = register_citations(
            f"Cited.{marker('turn0view0')}\n\nSee {self.GUIDE_LINK} too.",
            SOURCES,
            unresolved_label=UNRESOLVED,
        )
        body, citations = finalize_citations(registered.split("\n\n", 1)[1], bundle)

        assert body == f"See {self.GUIDE_LINK} too."
        assert citations == []


class TestBodyDigest:
    """One digest definition, asserted on both sides of the boundary.

    SHA-256 over the body's UTF-16 code units, little-endian, no BOM - the
    bytes ``charCodeAt`` yields in the browser and ``surrogatepass`` yields
    here. The vectors live in a fixture the renderer's test reads too, so the
    two implementations are held to the same table rather than to each other's
    reputation.
    """

    CASES = json.loads(
        (Path(__file__).parent / "fixtures" / "citation_body_digest.json").read_text(
            encoding="utf-8"
        )
    )

    @pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
    def test_the_backend_computes_the_digest_the_renderer_will_check(self, case):
        assert body_digest(case["text"]) == case["sha256"]

    @pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
    def test_the_recorded_digest_is_sha256_of_the_bytes_charcodeat_yields(self, case):
        assert case["sha256"] == hashlib.sha256(charcodeat_bytes(case["text"])).hexdigest()


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


class TestAuthorityBracketsAcrossTheBoundary:
    """The citation's column of the shared authority table.

    ``[`` and ``]`` are an IPv6 host's syntax and data everywhere else, and
    ``_canonical_uri`` writes every bracket it is handed as ``%5B``/``%5D``.
    Asking afterwards which ones to restore is asking a string that no longer
    holds the answer: ``https://[::1]/admin`` and ``https://%5B::1%5D/admin``
    arrive at the same spelling, and putting brackets back into both hands the
    second one a live loopback address its source never named. So the question
    is settled on the resolved destination, before spelling, and the answer is
    carried through.

    The Web renderer and the Slack adapter are measured on the same rows from
    their own side - see the fixture's description - because three surfaces
    agreeing on a repair is agreement, not proof.
    """

    FIXTURE = json.loads(
        (Path(__file__).resolve().parent / "fixtures/citation_authority_matrix.json").read_text(
            encoding="utf-8"
        )
    )
    CASES = FIXTURE["cases"]

    @pytest.mark.parametrize("case", CASES, ids=[c["why"] for c in CASES])
    def test_only_a_valid_literal_authority_keeps_its_brackets(self, case):
        assert safe_url(case["destination"]) == (case["safe_url"] or "")

    @pytest.mark.parametrize(
        "case", [c for c in CASES if c["safe_url"]], ids=[c["why"] for c in CASES if c["safe_url"]]
    )
    def test_the_accepted_spelling_is_still_a_fixed_point(self, case):
        """A restored bracket must not be spelled away by the next pass."""
        assert safe_url(case["safe_url"]) == case["safe_url"]
