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
from html.entities import html5 as HTML5_ENTITIES
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import unquote, urlsplit

from core.reply_enhancer import mask_hidden_and_code

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

# A CommonMark character reference, which is what a link destination resolves
# before it becomes an href. Providers that lifted a URL out of HTML hand over
# ``&amp;`` for ``&``, so one round of this is applied on the way in - and the
# same grammar decides, on the way out, which ``&`` has to be escaped so the
# renderer cannot resolve it a second time.
_REFERENCE_RE = re.compile(
    r"&(#[0-9]{1,7}|#[Xx][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});"
)
# The characters micromark's ``normalizeUri`` leaves alone. Everything else it
# percent-encodes, so writing anything else verbatim would make the delivered
# href differ from the sidecar URL and the badge would stop matching its link.
_URI_SAFE_RE = re.compile(r"[!#$&-;=?-Z_a-z~]")
# ``%`` followed by two ASCII alphanumerics is kept as an existing escape.
_URI_ESCAPE_RE = re.compile(r"%[0-9A-Za-z]{2}")
# Markdown's own link punctuation. ``normalizeUri`` keeps parentheses, but an
# unbalanced one truncates the link, so they are encoded here as well - and
# ``%28``/``%29`` survive it untouched, which keeps the result a fixed point.
_MARKDOWN_UNSAFE = frozenset("()")
# WHATWG forbidden domain code points, checked after percent-decoding: these are
# where ``urlsplit`` and a browser stop agreeing about which part is the host.
_FORBIDDEN_DOMAIN = frozenset("\x00\t\n\r #%/:<>?@[\\]^|\x7f")
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


def _resolve_references(value: str) -> str:
    """Resolve one round of CommonMark character references."""
    if "&" not in value:
        return value

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if body[0] == "#":
            try:
                code = int(body[2:], 16) if body[1] in "xX" else int(body[1:])
            except ValueError:
                return match.group(0)
            # CommonMark maps an out-of-range or surrogate code point to U+FFFD.
            if code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                return "\ufffd"
            return chr(code)
        return HTML5_ENTITIES.get(f"{body};", match.group(0))

    return _REFERENCE_RE.sub(replace, value)


def _percent_encode(char: str) -> str:
    """Percent-encode one character the way ``encodeURIComponent`` would."""
    if "\ud800" <= char <= "\udfff":
        # A lone surrogate is unrepresentable; the renderer substitutes U+FFFD,
        # so writing anything else would not survive it.
        char = "\ufffd"
    return "".join(f"%{byte:02X}" for byte in char.encode("utf-8", "replace"))


def _canonical_uri(value: str) -> str:
    """The destination form that survives the Markdown pipeline unchanged.

    ``remark`` resolves a destination's references and escapes, then percent-
    encodes what is left. Both are applied here so the URL the backend persists
    is byte-identical to the href the renderer produces from it - which is the
    only reason a badge can be matched to its own link.
    """
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "%":
            existing = _URI_ESCAPE_RE.match(value, index)
            if existing:
                out.append(existing.group(0))
                index = existing.end()
                continue
            out.append("%25")
        elif char == "&" and _REFERENCE_RE.match(value, index):
            # Left alone this would be resolved again on the way out, turning
            # one delivered URL into a different one.
            out.append("%26")
        elif char in _MARKDOWN_UNSAFE or not _URI_SAFE_RE.fullmatch(char):
            out.append(_percent_encode(char))
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _browser_host(url: str) -> str:
    """The host a browser resolves for *url*, or ``""`` if that is not knowable.

    ``urlsplit`` and the WHATWG parser split an authority differently once it
    carries a character a URL may not hold literally - a backslash is the host
    separator to one and userinfo to the other - so the host is read from the
    canonical form and rejected outright when the two could still disagree.
    """
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    if not host:
        return ""
    try:
        decoded = unquote(host, errors="strict")
    except UnicodeDecodeError:
        return ""
    if any(char in _FORBIDDEN_DOMAIN for char in decoded):
        return ""
    if decoded.isascii():
        return decoded
    try:
        # A browser labels an internationalized host by its punycode form, so a
        # citation has to attribute that and not the bytes the URL spells.
        return decoded.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return ""


def safe_url(value: Any) -> str:
    """Canonical http(s) URL usable as a Markdown destination, or ``""``.

    Anything else - ``javascript:``, ``data:``, ``file:``, a hostless URL, an
    authority whose host a browser would read differently - is rejected rather
    than repaired, so the citation is reported as unresolved instead of becoming
    an unsafe or misattributed link.
    """
    if not isinstance(value, str):
        return ""
    raw = _CONTROL_RE.sub("", _resolve_references(_CONTROL_RE.sub("", value))).strip()
    if not raw:
        return ""
    url = _canonical_uri(raw)
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return ""
    if scheme not in _ALLOWED_SCHEMES or not _browser_host(url):
        return ""
    return url


def source_label(url: str, title: str = "") -> str:
    """Short link text for a citation: its domain, which is what attributes it."""
    host = _browser_host(url).lower()
    if host.startswith("www."):
        host = host[4:]
    return (host or title)[:_LABEL_MAX].strip()


def _escape_label(value: str) -> str:
    """Escape the characters that would end a Markdown link label early."""
    return re.sub(r"([\\\[\]])", r"\\\1", value)


def has_citation_markers(text: Optional[str]) -> bool:
    """True when ``text`` carries at least one complete citation marker."""
    return bool(text) and _START in text and bool(CITATION_MARKER_RE.search(text))


def citation_ref_ids(text: Optional[str]) -> list[str]:
    """The refs a message asks to have attributed, in first-appearance order.

    Only markers the reader will actually see are requests: one inside a code
    example is literal text, and one inside a hidden block leaves with the
    block. A malformed ref is not a request either - it can name no source, so
    the marker degrades to the unresolved label no matter what arrives later.
    """
    if not text or _START not in text:
        return []
    requested: dict[str, None] = {}
    for match in CITATION_MARKER_RE.finditer(mask_hidden_and_code(text)):
        for ref in match.group(1).split(_SEP):
            if ref and _REF_ID_RE.fullmatch(ref):
                requested.setdefault(ref, None)
    return list(requested)


def unresolved_refs(
    ref_ids: Iterable[str], sources: Mapping[str, CitationSource]
) -> list[str]:
    """Which of *ref_ids* no source has been recorded for yet, in the given order.

    This is the question a delivery boundary asks: could a search still arrive
    that changes how this message reads? A ref whose source is already present
    has reached its final form even when that source turns out to be unlinkable -
    waiting on it would hold the message forever - and a malformed ref can never
    name a source at all, so neither is reported here.
    """
    return [ref for ref in ref_ids if _REF_ID_RE.fullmatch(ref) and ref not in sources]


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
    """Apply ``replace`` to every marker the reader will actually be shown.

    A marker shown inside a code example must stay literal - an agent
    explaining this very grammar is a case seen in real transcripts - and
    "inside code" is a question only a CommonMark lexer can answer. Fence
    lengths nest (a four-backtick block quoting a three-backtick one), an
    unclosed fence swallows the rest of the document, a code span may run
    across lines, and container indentation shifts all of it. A marker inside a
    ``<silent>`` block has to stay literal for a different reason: the block is
    removed before delivery, so rewriting it would spend a citation index on
    text nobody reads and lift a URL out of the block meant to hide it. Both
    decisions are delegated to the reply parser's offset-preserving mask:
    markers are matched against the mask and spliced back into the original
    source, which leaves every byte this function does not replace exactly as
    it arrived.
    """
    mask = mask_hidden_and_code(text)
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
