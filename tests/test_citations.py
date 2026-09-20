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

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.citations import (
    CitationSource,
    clean_title,
    has_citation_markers,
    resolve_citations,
    safe_url,
    source_label,
)

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
        ["http://example.com/x", "https://example.com/x", "HTTPS://Example.com/X"],
    )
    def test_http_and_https_are_accepted_verbatim(self, url):
        assert safe_url(url) == url

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
