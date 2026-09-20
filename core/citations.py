"""Source citations shared by agent backends.

Some model backends answer with opaque inline citation markers instead of
links. Codex (the OpenAI app-server) emits

    U+E200 "cite" U+E202 <ref_id> [U+E202 <ref_id> ...] U+E201

where each ``ref_id`` names one result of a web search performed earlier in the
same native thread. The results themselves arrive in separate notifications, so
a backend harvests them into a map scoped to that thread and hands the map here
when a message is about to be emitted.

This module owns the shared half of the feature: the marker grammar, the
sanitizing of untrusted titles and URLs, and the rewrite into ordinary Markdown
links plus a structured sidecar. It is deliberately transport-agnostic - the
Markdown text is what every IM platform delivers, and the sidecar is what the
Web renderer upgrades into compact citation badges.

Two rules shape every branch below: a citation we cannot resolve is *downgraded*
to a visible label, never silently dropped, and a URL is never invented from a
ref_id or from search order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from core.reply_enhancer import mask_markdown_code

# The private-use delimiters the marker is wrapped in.
_START = "\ue200"
_SEP = "\ue202"
_END = "\ue201"

# One complete marker. The payload may not contain a start/end delimiter, so an
# unterminated marker never matches: a stream that split a marker across chunks
# keeps its raw text instead of swallowing the answer around it. Requiring the
# literal ``cite`` keyword also keeps sibling U+E200 markers (``visualize``)
# untouched.
CITATION_MARKER_RE = re.compile(f"{_START}cite{_SEP}([^{_START}{_END}]*){_END}")

# Real ref_ids look like ``turn0view1``. Keep the shape permissive but bounded so
# a malformed payload falls through to the unresolved fallback.
_REF_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")

_ALLOWED_SCHEMES = frozenset({"http", "https"})
# C0/C1 controls, zero-width and line/paragraph separators, and the whole
# private-use area (which is where the markers themselves live).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\ue000-\uf8ff]")
# Markdown's own link punctuation: percent-encoded in the destination so a URL
# carrying parentheses (Wikipedia, docs anchors) cannot truncate the link.
_URL_ESCAPES = {"(": "%28", ")": "%29", " ": "%20", "<": "%3C", ">": "%3E", '"': "%22"}
_TITLE_MAX = 200
_LABEL_MAX = 64


@dataclass(frozen=True)
class CitationSource:
    """One citable search result, as the backend received it (untrusted)."""

    ref_id: str
    title: str = ""
    url: str = ""


@dataclass(frozen=True)
class Citation:
    """A resolved citation, numbered in first-appearance order within a message."""

    index: int
    ref_id: str
    title: str
    url: str
    label: str

    def to_payload(self) -> dict[str, Any]:
        """The persisted sidecar shape (``message.content.citations`` entries)."""
        return {
            "index": self.index,
            "ref_id": self.ref_id,
            "title": self.title,
            "url": self.url,
            "label": self.label,
        }


def clean_title(value: Any) -> str:
    """Collapse an untrusted source title to one safe single-line string."""
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL_RE.sub(" ", value).split())[:_TITLE_MAX].strip()


def safe_url(value: Any) -> str:
    """Canonical http(s) URL usable as a Markdown destination, or ``""``.

    Anything else - ``javascript:``, ``data:``, ``file:``, a hostless URL, an
    unparseable authority - is rejected rather than repaired, so the citation is
    reported as unresolved instead of becoming an unsafe link.
    """
    if not isinstance(value, str):
        return ""
    raw = _CONTROL_RE.sub("", value).strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        host = parts.hostname
    except ValueError:
        return ""
    if scheme not in _ALLOWED_SCHEMES or not host:
        return ""
    return "".join(_URL_ESCAPES.get(ch, ch) for ch in raw)


def source_label(url: str, title: str = "") -> str:
    """Short link text for a citation: its domain, which is what attributes it."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    return (host or title)[:_LABEL_MAX].strip()


def _escape_label(value: str) -> str:
    """Escape the characters that would end a Markdown link label early."""
    return re.sub(r"([\\\[\]])", r"\\\1", value)


def has_citation_markers(text: Optional[str]) -> bool:
    """True when ``text`` carries at least one complete citation marker."""
    return bool(text) and _START in text and bool(CITATION_MARKER_RE.search(text))


def resolve_citations(
    text: Optional[str],
    sources: Mapping[str, CitationSource],
    *,
    unresolved_label: str,
) -> tuple[Optional[str], list[dict[str, Any]]]:
    """Rewrite citation markers into Markdown links plus a structured sidecar.

    ``sources`` maps ref_id to the search result the backend captured for it,
    scoped to the native thread the message belongs to. Returns the text to
    deliver and the sidecar payload in first-appearance order; sources repeated
    across markers share one entry (and one index) keyed by their URL.

    A marker whose sources are all unknown or unlinkable becomes
    ``unresolved_label``; a marker where only some refs resolve keeps the links
    it has and adds the label once. When there is no label to fall back to, the
    raw marker is left in place - the answer text is never quietly de-attributed.
    """
    if not text or _START not in text:
        return text, []

    fallback = (unresolved_label or "").strip()
    citations: list[Citation] = []
    by_url: dict[str, Citation] = {}

    def resolve_ref(ref: str) -> Optional[Citation]:
        if not _REF_ID_RE.fullmatch(ref):
            return None
        source = sources.get(ref)
        if source is None:
            return None
        url = safe_url(source.url)
        if not url:
            return None
        existing = by_url.get(url)
        if existing is not None:
            return existing
        title = clean_title(source.title)
        label = source_label(url, title)
        if not label:
            return None
        citation = Citation(
            index=len(citations) + 1,
            ref_id=ref,
            title=title,
            url=url,
            label=label,
        )
        citations.append(citation)
        by_url[url] = citation
        return citation

    def replace(match: re.Match[str]) -> str:
        refs = [ref for ref in match.group(1).split(_SEP) if ref]
        links: list[str] = []
        unresolved = not refs
        for ref in refs:
            citation = resolve_ref(ref)
            if citation is None:
                unresolved = True
                continue
            links.append(f"[{_escape_label(citation.label)}]({citation.url})")
        if unresolved and fallback:
            links.append(fallback)
        if not links:
            return match.group(0)
        # Markers usually sit flush against the preceding word or full stop;
        # a separating space keeps the link from reading as part of the sentence.
        lead = "" if match.start() == 0 or text[match.start() - 1].isspace() else " "
        return lead + " ".join(links)

    return _rewrite_outside_code(text, replace), [c.to_payload() for c in citations]


def _rewrite_outside_code(text: str, replace: Callable[[re.Match[str]], str]) -> str:
    """Apply ``replace`` to every marker CommonMark does not read as code.

    A marker shown inside a code example must stay literal - an agent
    explaining this very grammar is a case seen in real transcripts - and
    "inside code" is a question only a CommonMark lexer can answer. Fence
    lengths nest (a four-backtick block quoting a three-backtick one), an
    unclosed fence swallows the rest of the document, a code span may run
    across lines, and container indentation shifts all of it. So the decision
    is delegated to the reply parser's offset-preserving mask: markers are
    matched against the mask and spliced back into the original source, which
    leaves every byte this function does not replace exactly as it arrived.
    """
    mask = mask_markdown_code(text)
    out: list[str] = []
    cursor = 0
    # A match in the mask cannot overlap a blanked region, so the marker text
    # under it is the original text - only its surroundings may have been
    # blanked, and those are copied from ``text``, never from the mask.
    for match in CITATION_MARKER_RE.finditer(mask):
        out.append(text[cursor : match.start()])
        out.append(replace(match))
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out)
