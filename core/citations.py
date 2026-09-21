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
from dataclasses import dataclass, replace as replace_dataclass
from html.entities import html5 as HTML5_ENTITIES
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import unquote

import idna

from core.reply_enhancer import hidden_block_ranges, mask_hidden_and_code

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
# C0/C1 controls, line/paragraph separators, and the whole private-use area
# (which is where the markers themselves live), plus the invisible formatting
# characters a single-line source title has no legitimate use for: the bidi
# marks, embeddings, overrides and isolates that let a title render its own
# text backwards, and the zero-width space, word joiner and byte order mark.
#
# ZWNJ, ZWJ and the variation selectors are deliberately left in: they carry
# meaning inside a real title, joining an emoji sequence or spelling an Indic
# or Persian word, and none of them can reorder what the reader sees.
_CONTROL_RE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u061c\u200b\u200e\u200f\u202a-\u202e"
    "\u2028\u2029\u2060\u2066-\u2069\ufeff\ue000-\uf8ff]"
)
# What WHATWG's URL parser deletes from its input before parsing anything: a tab
# or newline anywhere, and C0 controls or spaces at either end.
_URL_REMOVED_RE = re.compile(r"[\t\n\r]")
_URL_TRIMMED = "".join(chr(code) for code in range(0x21))
_DECIMAL_DIGITS = frozenset("0123456789")
_OCTAL_DIGITS = frozenset("01234567")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
# The widest an IPv4 number can be written per radix and still fit in 32 bits
# (``4294967295``, ``0xffffffff``, ``0o37777777777``), and the stand-in for one
# written wider. Every check that consumes a number rejects a value this big, so
# the exact magnitude past the cap never matters - and not converting it keeps
# CPython's bound on decimal ``int`` conversion out of an untrusted host.
_IPV4_WIDEST = {10: 10, 16: 8, 8: 11}
_IPV4_TOO_LARGE = 1 << 32

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
# WHATWG forbidden domain code points, checked after percent-decoding. The C0
# range is there in full: a host is not allowed to hold any of it, and a
# citation whose host carries one names a page no browser opens.
_FORBIDDEN_DOMAIN = frozenset(
    "".join(chr(code) for code in range(0x20)) + " #%/:<>?@[\\]^|\x7f"
)
# The ports a browser drops from the URL it shows, because they are the ones it
# would have used anyway.
_DEFAULT_PORTS = {"http": 80, "https": 443}
_PORT_MAX = 65535
_PORT_DIGITS = len(str(_PORT_MAX))
# An IPv6 host is wrapped in brackets, which ``_canonical_uri`` percent-encodes
# along with every other character Markdown would read as syntax - so the
# wrapper is recognized in either spelling and always written back literally.
_IPV6_OPEN_RE = re.compile(r"\[|%5[Bb]")
_IPV6_CLOSE_RE = re.compile(r"\]|%5[Dd]")
# Where an authority ends. A backslash ends one too for a special scheme, but
# ``_canonical_uri`` has already percent-encoded it by this point.
_AUTHORITY_END_RE = re.compile(r"[/?#]")
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
    # The exact Markdown link this citation writes, character for character,
    # and which of that spelling's occurrences in the delivered text are its
    # own. See ``_link_provenance``.
    spelling: str = ""
    occurrences: tuple[int, ...] = ()
    occurrence_total: int = 0

    def to_payload(self) -> dict[str, Any]:
        """The persisted sidecar shape (``message.content.citations`` entries)."""
        return {
            "index": self.index,
            "ref_id": self.ref_id,
            "title": self.title,
            "url": self.url,
            "label": self.label,
            "spelling": self.spelling,
            "occurrences": list(self.occurrences),
            "occurrence_total": self.occurrence_total,
        }


def clean_title(value: Any) -> str:
    """Collapse an untrusted source title to one safe single-line string."""
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL_RE.sub(" ", value).split())[:_TITLE_MAX].strip()


def _is_replaced_code_point(code: int) -> bool:
    """Whether the renderer substitutes U+FFFD for the numeric reference *code*.

    This is micromark's table (``micromark-util-decode-numeric-character-
    reference``), because micromark is the parser behind the Web renderer and
    the only table that can keep a persisted URL equal to the href drawn from
    it. It is wider than CommonMark's own wording, which replaces just NUL,
    surrogates and out-of-range values: micromark also replaces the C0 controls
    a URL may not hold, the whole C1 range, and the noncharacters. Notably it
    does *not* apply HTML's Windows-1252 mapping, so ``&#x80;`` becomes U+FFFD
    rather than ``€`` - resolving it to ``€`` would name a page the rendered
    link never opens.
    """
    return (
        code < 9
        or code == 11
        or 13 < code < 32
        or 126 < code < 160
        or 55295 < code < 57344
        or 64975 < code < 65008
        or code & 0xFFFF in (0xFFFE, 0xFFFF)
        or code > 0x10FFFF
    )


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
            return "\ufffd" if _is_replaced_code_point(code) else chr(code)
        return HTML5_ENTITIES.get(f"{body};", match.group(0))

    return _REFERENCE_RE.sub(replace, value)


def _normalize_destination(value: str) -> str:
    """The provider's URL string as a browser reads it, before Markdown escaping.

    The two passes are ordered the way the pipeline applies them, because they
    disagree about the same character. A *literal* tab or newline is cleaned off
    first, the way WHATWG's URL parser cleans its own input - removed wherever it
    sits, with leading and trailing C0 controls and spaces trimmed - and it has
    to be: a literal tab in a Markdown destination ends the destination, so
    ``[x](https://example.com/p<TAB>q)`` renders as no link at all.

    A tab a *character reference* spells is a different character. Providers lift
    URLs out of HTML, so one round of references is resolved after that cleanup,
    and what it produces is part of the destination: micromark percent-encodes
    it, so ``p&Tab;q``, ``p&#x9;q`` and ``p&#xA;q`` have the hrefs ``p%09q``,
    ``p%09q`` and ``p%0Aq``. Resolving before the cleanup deleted exactly those,
    pointing the citation at ``p&#x9;q`` rendered as ``pq`` - a different page,
    silently.

    Everything else a reference produces survives to be percent-encoded too,
    because that is what the browser does with it. Deleting it instead - which a
    blanket control scrub either side of the reference pass used to do - moved
    the page the same way: ``https://example.com/p&#x80;q`` became
    ``https://example.com/pq`` rather than the ``p%EF%BF%BDq`` the renderer
    produces.
    """
    return _resolve_references(_URL_REMOVED_RE.sub("", value).strip(_URL_TRIMMED))


def _ipv4_number(part: str) -> Optional[int]:
    """One dotted part as WHATWG's IPv4 number parser reads it, or ``None``.

    A leading ``0x`` makes the part hexadecimal and a bare leading ``0`` makes
    it octal, which is how ``0x7f.1`` and ``017700000001`` both reach
    ``127.0.0.1``.

    A part written wider than 32 bits is still a number - a browser reads it and
    then fails the address - so it answers with a value past the cap rather than
    with ``None``, which would call it a domain name instead. That also keeps
    ``int`` off a host spelled with thousands of digits, which CPython refuses to
    convert at all (``sys.set_int_max_str_digits``): an unhandled ``ValueError``
    there would have escaped ``safe_url`` on nothing but an untrusted string.
    """
    if not part:
        return None
    digits, radix = _DECIMAL_DIGITS, 10
    if len(part) > 1 and part[0] == "0":
        if part[1] in "xX":
            part, radix, digits = part[2:], 16, _HEX_DIGITS
            if not part:
                return 0
        else:
            part, radix, digits = part[1:], 8, _OCTAL_DIGITS
    if not all(char in digits for char in part):
        return None
    if len(part.lstrip("0")) > _IPV4_WIDEST[radix]:
        return _IPV4_TOO_LARGE
    return int(part, radix)


def _canonical_host(host: str) -> str:
    """*host* as a browser serializes it, or ``""`` when it names no host.

    WHATWG hands any host whose last label is a number to its IPv4 parser, so
    ``2130706433``, ``0x7f.1``, ``127.1`` and ``017700000001`` all name
    ``127.0.0.1``. An untrusted search result that spells a loopback or
    private-network address that way would otherwise be attributed to the digits
    themselves, hiding where the link goes; and a form the parser refuses
    (``256.1.1.1``, ``1.2.3.4.5``, ``example.com.0x1``) names no host at all.
    """
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        # A trailing dot is not a label. A browser keeps it on a domain
        # (``example.com.``) but ignores it when reading a number.
        parts = parts[:-1]
    last = parts[-1]
    ends_in_number = bool(last) and all(char in _DECIMAL_DIGITS for char in last)
    if not ends_in_number and _ipv4_number(last) is None:
        return host
    if len(parts) > 4:
        return ""
    numbers: list[int] = []
    for part in parts:
        number = _ipv4_number(part)
        if number is None:
            return ""
        numbers.append(number)
    if any(number > 255 for number in numbers[:-1]):
        return ""
    if numbers[-1] >= 256 ** (5 - len(numbers)):
        return ""
    address = numbers[-1]
    for offset, number in enumerate(numbers[:-1]):
        address += number * 256 ** (3 - offset)
    return ".".join(str((address >> shift) & 0xFF) for shift in (24, 16, 8, 0))


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


def _parse_ipv6(value: str) -> Optional[list[int]]:
    """WHATWG's IPv6 parser: eight 16-bit pieces, or ``None`` for no address.

    Python's own ``ipaddress`` cannot stand in here. It accepts a zone id
    (``fe80::1%eth0``) that a browser refuses, and it serializes an
    IPv4-mapped address back as ``::ffff:127.0.0.1`` where a browser shows
    ``::ffff:7f00:1`` - so a label drawn from it would name the address bar
    shows something the reader never sees.
    """
    address = [0] * 8
    piece_index = 0
    compress: Optional[int] = None
    pointer = 0
    length = len(value)

    def char(offset: int = 0) -> str:
        position = pointer + offset
        return value[position] if position < length else ""

    if char() == ":":
        if char(1) != ":":
            return None
        pointer += 2
        piece_index += 1
        compress = piece_index
    while pointer < length:
        if piece_index == 8:
            return None
        if char() == ":":
            if compress is not None:
                return None
            pointer += 1
            piece_index += 1
            compress = piece_index
            continue
        piece_value = 0
        piece_length = 0
        while piece_length < 4 and char() in _HEX_DIGITS:
            piece_value = piece_value * 0x10 + int(char(), 16)
            pointer += 1
            piece_length += 1
        if char() == ".":
            # A dotted tail, as in ``::ffff:127.0.0.1``. It is read as an IPv4
            # address and stored in the last two pieces, which is why the
            # browser serializes it back as hexadecimal.
            if piece_length == 0 or piece_index > 6:
                return None
            pointer -= piece_length
            numbers_seen = 0
            while pointer < length:
                ipv4_piece: Optional[int] = None
                if numbers_seen > 0:
                    if char() == "." and numbers_seen < 4:
                        pointer += 1
                    else:
                        return None
                if char() not in _DECIMAL_DIGITS:
                    return None
                while char() in _DECIMAL_DIGITS:
                    number = int(char())
                    if ipv4_piece is None:
                        ipv4_piece = number
                    elif ipv4_piece == 0:
                        return None
                    else:
                        ipv4_piece = ipv4_piece * 10 + number
                    if ipv4_piece > 255:
                        return None
                    pointer += 1
                address[piece_index] = address[piece_index] * 0x100 + (ipv4_piece or 0)
                numbers_seen += 1
                if numbers_seen in (2, 4):
                    piece_index += 1
            if numbers_seen != 4:
                return None
            break
        if char() == ":":
            pointer += 1
            if pointer >= length:
                return None
        elif char():
            return None
        address[piece_index] = piece_value
        piece_index += 1
    if compress is not None:
        swaps = piece_index - compress
        piece_index = 7
        while piece_index != 0 and swaps > 0:
            address[piece_index], address[compress + swaps - 1] = (
                address[compress + swaps - 1],
                address[piece_index],
            )
            piece_index -= 1
            swaps -= 1
    elif piece_index != 8:
        return None
    return address


def _serialize_ipv6(address: list[int]) -> str:
    """The address as a browser writes it: lowercase, longest zero run elided."""
    compress: Optional[int] = None
    best_length = 1
    run_start: Optional[int] = None
    run_length = 0
    for piece_index in range(8):
        if address[piece_index] != 0:
            run_start = None
            run_length = 0
            continue
        if run_start is None:
            run_start = piece_index
        run_length += 1
        # Strictly greater keeps the leftmost of two equally long runs.
        if run_length > best_length:
            best_length = run_length
            compress = run_start

    out: list[str] = []
    ignore_zero = False
    for piece_index in range(8):
        if ignore_zero and address[piece_index] == 0:
            continue
        ignore_zero = False
        if compress == piece_index:
            out.append("::" if piece_index == 0 else ":")
            ignore_zero = True
            continue
        out.append(format(address[piece_index], "x"))
        if piece_index != 7:
            out.append(":")
    return "".join(out)


def _canonical_domain(host: str) -> str:
    """A non-IP host as a browser serializes it, or ``""`` when it names none.

    An internationalized label is canonicalized the way a browser does it:
    non-transitional UTS #46, with the STD3 and hyphen rules off. Python's own
    ``"idna"`` codec is IDNA 2003 *transitional* instead, which folds ``ß`` into
    ``ss`` - so ``https://faß.de/`` would be attributed to ``fass.de`` while the
    link opens ``xn--fa-hia.de``, naming a domain the reader never visits.
    """
    if not host:
        return ""
    try:
        decoded = unquote(host, errors="strict")
    except UnicodeDecodeError:
        return ""
    if any(char in _FORBIDDEN_DOMAIN for char in decoded):
        return ""
    labels: list[str] = []
    for label in decoded.split("."):
        if label.isascii():
            # A browser passes an ASCII label through untouched apart from case,
            # even one UTS #46 refuses: ``my_site.example.com`` holds an
            # underscore and ``ab--cd.example`` trips the hyphen rule, and both
            # resolve. Running them through IDNA would lose the citation.
            labels.append(label.lower())
            continue
        try:
            # A browser labels an internationalized host by the punycode it
            # actually visits, so a citation has to attribute that and not the
            # bytes the URL spells.
            labels.append(
                idna.encode(
                    label, uts46=True, transitional=False, std3_rules=False
                ).decode("ascii")
            )
        except (UnicodeError, ValueError):
            # Stricter than a browser on the margins: IDNA 2008 disallows symbol
            # labels UTS #46 alone would map (``❤.example`` resolves for a
            # browser, and is dropped here). Rejection degrades the citation to
            # its unresolved label, which is the only safe direction - a label
            # that names a different domain than the link opens is the defect.
            return ""
    return _canonical_host(".".join(labels))


def _port_accepted(value: str) -> bool:
    """Whether a browser would read *value* as a port at all.

    ``str.isdigit`` is true for ``٣`` and ``int`` reads that as 3, so a port
    checked with it would let ``:٣`` through as 3 while a browser refuses the
    URL outright. Membership in the ASCII digits is the only test that agrees.

    An empty port is not a rejection - ``https://example.com:/x`` opens - and a
    port in range is kept as the provider wrote it, default or not: it is the
    same page either way, and the label never shows a port.
    """
    if not value:
        return True
    if not all(char in _DECIMAL_DIGITS for char in value):
        return False
    trimmed = value.lstrip("0")
    # Bound the string before converting it: CPython refuses to convert a
    # decimal ``int`` past ``sys.set_int_max_str_digits``, and an untrusted
    # authority is exactly where a host spelled with thousands of digits shows
    # up. Anything wider than the maximum port is out of range regardless.
    if len(trimmed) > _PORT_DIGITS:
        return False
    return not trimmed or int(trimmed) <= _PORT_MAX


def _authority(url: str, scheme: str) -> Optional[tuple[str, str, str]]:
    """``(authority, label host, rest)`` for *url*, or ``None`` to reject it.

    One function answers the whole authority, because the parts are not
    independent: which ``@`` starts the host depends on the userinfo, whether a
    ``:`` starts a port depends on whether the host is a bracketed IPv6
    literal, and whether a port is in range depends on nothing else at all.
    Asking those questions separately - which is what reading
    ``urlsplit().hostname`` and nothing else amounted to - answers each one
    against a different authority, and every shape that fell between two of
    them became a citation pointing somewhere the reader could not go.

    It answers two things at once because they are one judgment. Whether a
    browser can open this URL decides if the citation is delivered; what that
    browser resolves the host to decides what the label may claim. The
    *authority* it hands back is still the one the provider wrote: a page is
    named by the URL its source gave, and a host written ``2130706433``,
    ``ＥＸＡＭＰＬＥ.com`` or ``:0080`` opens the same page the label names.

    The canonical URI is what gets parsed, not the provider's raw string, so
    this reads the same authority the renderer will hand the browser.
    """
    remainder = url[len(scheme) + 1 :]
    if not remainder.startswith("//"):
        return None
    remainder = remainder[2:]
    end = _AUTHORITY_END_RE.search(remainder)
    authority = remainder[: end.start()] if end else remainder
    rest = remainder[end.start() :] if end else ""

    # WHATWG splits userinfo at the LAST ``@``: everything before it is
    # credentials, which may hold an ``@`` of their own. They are carried
    # across untouched - case-folding a password would change it.
    userinfo, separator, host_port = authority.rpartition("@")
    if not separator:
        userinfo, host_port = "", authority

    opener = _IPV6_OPEN_RE.match(host_port)
    if opener is not None:
        closer = _IPV6_CLOSE_RE.search(host_port, opener.end())
        if closer is None:
            return None
        inner = host_port[opener.end() : closer.start()]
        address = _parse_ipv6(inner)
        if address is None:
            return None
        # Literal brackets, not the ``%5B`` the URI encoder would leave: a
        # destination spelled that way is one the URL parser refuses outright,
        # so the badge could not open the address either.
        host = f"[{inner}]"
        label_host = f"[{_serialize_ipv6(address)}]"
        port = host_port[closer.end() :]
        if port and not port.startswith(":"):
            return None
    else:
        host, colon, port_text = host_port.partition(":")
        label_host = _canonical_domain(host)
        if not label_host:
            return None
        port = f":{port_text}" if colon else ""

    if not _port_accepted(port[1:]):
        return None
    authority = f"{host}{port}"
    return (f"{userinfo}@{authority}" if userinfo else authority), label_host, rest


def safe_url(value: Any) -> str:
    """Canonical http(s) URL usable as a Markdown destination, or ``""``.

    Anything else - ``javascript:``, ``data:``, ``file:``, a hostless URL, an
    authority no browser would open - is rejected rather than repaired, so the
    citation is reported as unresolved instead of becoming an unsafe or dead
    link.

    What survives is left as it arrived, percent-encoding aside: the URL names
    the page its source gave, and this is not the place to decide that two
    spellings of one are the same. ``source_label`` is where the host a browser
    resolves is worked out, because that is the one place the difference is
    something the reader is shown.
    """
    if not isinstance(value, str):
        return ""
    raw = _normalize_destination(value)
    if not raw:
        return ""
    url = _canonical_uri(raw)
    scheme, separator, _ = url.partition(":")
    # A browser lowercases the scheme, and a renderer that only knows the
    # lowercase spelling does not see a link at all: Telegram delivered
    # ``[example.com](HTTPS://Example.com/X)`` as raw Markdown.
    scheme = scheme.lower()
    if not separator or scheme not in _ALLOWED_SCHEMES:
        return ""
    parsed = _authority(url, scheme)
    if parsed is None:
        return ""
    return f"{scheme}://{parsed[0]}{parsed[2]}"


def source_label(url: str, title: str = "") -> str:
    """Short link text for a citation: its domain, which is what attributes it."""
    host = ""
    if isinstance(url, str):
        scheme, separator, _ = url.partition(":")
        if separator:
            parsed = _authority(url, scheme.lower())
            if parsed is not None:
                # The port is left out on purpose. A page is attributed by the
                # site it is on, a non-default port does not change which site
                # that is, and showing it would spend six of the label's 64
                # characters saying so.
                host = parsed[1]
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return title[:_LABEL_MAX].strip()
    if len(host) <= _LABEL_MAX:
        return host
    # Shorten a long host from the LEFT, keeping the tail verbatim: the label is
    # always either the host or a suffix of it, marked by the ellipsis.
    #
    # A label cut from the right names a site the link never opens -
    # ``developers.openai.com.<padding>.attacker.example`` shown as
    # ``developers.openai.com…`` - and keeping only whole labels degrades the
    # other way: ``<62 chars>.co.uk`` would be shown as ``…co.uk``, a public
    # suffix every site under it shares and the perfect place to hide a long
    # attacker label. So the budget is filled from the right instead, and only
    # the leftmost piece - the one the ellipsis is attached to - may be partial.
    #
    # That cannot compress a host into a different plausible domain, because the
    # two cases exclude each other. A short fragment only happens when whole
    # labels already fill most of the budget, and those are the rightmost labels,
    # so the registrable domain is shown whole and the fragment sits in a
    # subdomain slot. A fragment of the registrable label itself only happens
    # when little else fits, which leaves it most of the 63 characters - more
    # than a DNS label may hold minus a few, so nothing recognizable can hide in
    # what is left out.
    return "…" + host[-(_LABEL_MAX - 1) :]


# Every character CommonMark (plus GFM) still reads as syntax inside link
# text. Two different kinds of damage, one answer: a backtick, an angle bracket
# or an unbalanced square bracket opens a construct that runs past ``](url)``
# and swallows the link itself, while emphasis, strikethrough, a character
# reference or a table cell divider quietly change which characters the reader
# is shown. The label is the source's domain - the whole attribution claim - so
# a label that reads as ``ab.example`` when the link goes to ``a*b*.example``
# is the same defect as no link at all.
_LABEL_SYNTAX_RE = re.compile(r"([\\\[\]`<*_~&|])")


def _escape_label(value: str) -> str:
    """Escape every character a Markdown reader would not show verbatim.

    What comes out is a label that reads back exactly as the sidecar spells it
    on every surface. The Web renderer resolves the escapes itself; each IM
    formatter resolves them once, ahead of its own dialect pass, through
    ``modules.im.formatters.hold_markdown_escapes`` - so no reader is shown a
    backslash that was only ever there to keep a domain intact.
    """
    return _LABEL_SYNTAX_RE.sub(r"\\\1", value)


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

    Each sidecar entry also records which links in the delivered text it wrote,
    so a consumer can tell a citation apart from an ordinary link the answer
    happened to point at the same page.
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
            spelling=f"[{_escape_label(label)}]({url})",
        )
        citations.append(citation)
        by_url[url] = citation
        return citation

    # Where each link this rewrite writes lands in the delivered text, keyed by
    # the offset of the marker it replaced so the caller can add the offset the
    # replacement itself was spliced to.
    written: dict[int, list[tuple[int, Citation]]] = {}

    def replace(match: re.Match[str]) -> str:
        refs = [ref for ref in match.group(1).split(_SEP) if ref]
        links: list[str] = []
        placed: list[tuple[int, Citation]] = []
        unresolved = not refs
        # Markers usually sit flush against the preceding word or full stop;
        # a separating space keeps the link from reading as part of the sentence.
        lead = "" if match.start() == 0 or text[match.start() - 1].isspace() else " "
        cursor = len(lead)
        for ref in refs:
            citation = resolve_ref(ref)
            if citation is None:
                unresolved = True
                continue
            link = citation.spelling
            placed.append((cursor, citation))
            # One space joins the links below, so the next one starts past it.
            cursor += len(link) + 1
            links.append(link)
        if unresolved and fallback:
            links.append(fallback)
        if not links:
            return match.group(0)
        written[match.start()] = placed
        return lead + " ".join(links)

    rewritten, offsets = _rewrite_outside_code(text, replace)
    provenance = _link_provenance(
        rewritten,
        {
            offsets[marker_start] + relative: citation
            for marker_start, placed in written.items()
            for relative, citation in placed
        },
    )
    return rewritten, [
        c.to_payload()
        for c in (
            replace_dataclass(
                citation,
                occurrences=provenance[0].get(citation.spelling, ()),
                occurrence_total=provenance[1].get(citation.spelling, 0),
            )
            for citation in citations
        )
    ]


def _link_provenance(
    text: str,
    written: Mapping[int, Citation],
) -> tuple[dict[str, tuple[int, ...]], dict[str, int]]:
    """Which occurrences of its own link spelling each citation wrote.

    A badge is an attribution claim, so it has to belong to a link this module
    wrote - not to any link the answer's prose happens to point at the same
    page with. The delivered text is the only thing the producer and a consumer
    both hold, so provenance travels as a position in it.

    The position is counted over one exact string: the complete link this
    module wrote, ``[escaped label](url)``, character for character. Counting a
    literal substring is the one measurement two different Markdown
    implementations cannot disagree about - it asks nothing of either
    vocabulary. Counting by parsed destination did ask: a GFM footnote
    definition holding ``[p](url)`` is a link to one parser and part of a
    definition to another, and the citation lost its badge over the
    disagreement. So every occurrence of the spelling counts here, whether it
    is a link, a code example, or a footnote, and the consumer counts the same
    way over the same characters.

    What the reader is never handed is not counted, because the consumer never
    sees it either: a silent block is removed before delivery, so an occurrence
    inside one would put the producer one ahead for the rest of the message.
    Every other delivery transform was measured to leave the relative order of
    an exact spelling alone (see ``tests/test_citation_consumers.py``).

    The total is recorded alongside so a disagreement is still detectable. A
    consumer that counts a different number is looking at different text, and
    the honest answer there is the ordinary link the reader already has, not a
    badge on whichever link landed in that position.
    """
    hidden = hidden_block_ranges(text)

    def shown(offset: int) -> bool:
        return not any(start <= offset < end for start, end in hidden)

    ordinals: dict[str, dict[int, int]] = {}
    totals: dict[str, int] = {}
    for spelling in {citation.spelling for citation in written.values()}:
        seen: dict[int, int] = {}
        # Overlapping matches are searched for from one character in, not one
        # spelling on, so the two sides scan identically whatever the label
        # holds. A link spelling cannot actually overlap itself - it opens with
        # ``[`` and closes with ``)`` - but neither side has to prove that.
        at = text.find(spelling)
        while at != -1:
            if shown(at):
                seen[at] = len(seen) + 1
            at = text.find(spelling, at + 1)
        ordinals[spelling] = seen
        totals[spelling] = len(seen)

    occurrences: dict[str, list[int]] = {}
    for offset, citation in written.items():
        ordinal = ordinals[citation.spelling].get(offset)
        if ordinal is not None:
            occurrences.setdefault(citation.spelling, []).append(ordinal)
    return (
        {spelling: tuple(sorted(found)) for spelling, found in occurrences.items()},
        totals,
    )


def _rewrite_outside_code(
    text: str,
    replace: Callable[[re.Match[str]], str],
) -> tuple[str, dict[int, int]]:
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

    Also returns where each replacement was written, keyed by the offset of the
    marker it replaced, so a caller that cares which text it produced can find
    it without reparsing for its own output.
    """
    mask = mask_hidden_and_code(text)
    out: list[str] = []
    offsets: dict[int, int] = {}
    written = 0
    cursor = 0
    # A match in the mask cannot overlap a blanked region, so the marker text
    # under it is the original text - only its surroundings may have been
    # blanked, and those are copied from ``text``, never from the mask.
    for match in CITATION_MARKER_RE.finditer(mask):
        prefix = text[cursor : match.start()]
        replacement = replace(match)
        out.append(prefix)
        out.append(replacement)
        written += len(prefix)
        offsets[match.start()] = written
        written += len(replacement)
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out), offsets
