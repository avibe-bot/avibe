"""Reply parser for silent blocks, file attachments, and quick-reply buttons.

Extracts special syntaxes from agent reply text:

1. **Silent blocks** – ``<silent>...</silent>`` sections that are never forwarded
   to the IM user. If nothing remains after stripping them, no message is sent.

2. **File links** – Markdown links whose URL starts with ``file://``
   e.g. ``[screenshot](file:///tmp/shot.png)``

3. **Quick-reply buttons** – A ``---`` separator followed by
   ``[button text]`` tokens separated by ``|``
   e.g. ``---\\n[👌好的] | [✅提交PR] | [先review一下]``
"""

from __future__ import annotations

import logging
import ntpath
import os
import re
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import List, Tuple
from urllib.parse import unquote, urlparse

from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)
_MARKDOWN = MarkdownIt("commonmark")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FileLink:
    """A file reference extracted from agent reply text."""

    label: str  # Markdown link text (e.g. "screenshot")
    path: str  # Absolute local path (e.g. "/tmp/shot.png")
    is_image: bool = False  # True when parsed from ![alt](file://...)


@dataclass
class QuickReplyButton:
    """A quick-reply button extracted from the trailing block."""

    text: str  # Button label / reply text (e.g. "👌好的" or "好的")


@dataclass
class SecretRequest:
    """A ``$<NAME>`` dynamic-ask marker found in agent reply text (Vaults).

    The agent writes ``$<openAiKey>`` to ask the user for a secret; the value is
    filled through a trusted UI channel, never the chat. The marker stays in ``.text``
    so the web transcript can render it as a secure input card; IM replaces it with a
    deep link in the platform formatter.
    """

    name: str


@dataclass
class EnhancedReply:
    """Result of processing an agent reply through the enhancer."""

    text: str  # Cleaned message text (file links & button block removed)
    files: List[FileLink] = field(default_factory=list)
    buttons: List[QuickReplyButton] = field(default_factory=list)
    secret_requests: List[SecretRequest] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches markdown links with file:// URLs, including image links:
#   [label](file:///path)
#   ![alt](file:///path)
_FILE_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\((file://(?:[^()]+|\([^)]*\))+)\)")

# Matches the quick-reply button block at the end of the text.
# A horizontal rule (``---``) on its own line, followed by bracket buttons.
# Accept link-formatted variants defensively because agents routinely wrap a
# quick-reply label in a link — the label stays the payload, the URL is dropped.
# Tolerated link forms: plain Markdown ``[label](https://…)``, Slack-escaped
# ``[label](<https://…>)``, and Slack autolink ``<https://…|label>``. Only
# ``http(s)`` targets count (a bare ``[label](foo)`` is left alone).
#
# ``_PLAIN_URL`` allows one level of balanced parentheses (e.g. Wikipedia
# ``…/A_(B)``) like ``_FILE_LINK_RE`` does, so such a URL doesn't truncate at the
# first ``)`` and drop the rest of the button group.
_PLAIN_URL = r"https?://(?:[^()\s]|\([^()]*\))+"
# Optional link wrapper after a ``[label]`` token: ``(<https://…>)`` or ``(https://…)``.
_LINK_SUFFIX = r"(?:\((?:<https?://[^>\n]+>|" + _PLAIN_URL + r")\))?"

_BUTTON_BLOCK_RE = re.compile(
    r"\n-{3,}\s*\n"  # --- separator line
    r"((?:\s*(?:\[[^\]]+\]" + _LINK_SUFFIX + r"|<https?://[^|>\n]+\|[^>\n]+>)\s*(?:[|｜]\s*)?)+)"  # button tokens
    r"\s*$",  # trailing whitespace / end of string
)

# Individual button tokens. Link variants are accepted for compatibility only;
# the bracket label (or Slack link text) remains the quick-reply payload.
_BUTTON_TOKEN_RE = re.compile(
    r"\[([^\]]+)\]" + _LINK_SUFFIX + r"|<https?://[^|>\n]+\|([^>\n]+)>"
)

# A block that is *only* plain ``[label](https://…)`` links (one or more, with no
# ``|``/``｜`` separator and no bare/angle/Slack token) is a genuine reference-link
# section, not a button group — see ``_extract_buttons``. Plain links become
# buttons only when an explicit separator or another unambiguous token is present.
# (Detection matches the whole block instead of scanning for ``|``, which a URL
# may itself contain.)
_PLAIN_LINKS_ONLY_RE = re.compile(r"(?:\s*\[[^\]]+\]\(" + _PLAIN_URL + r"\)\s*)+")

# Silent output blocks are intentionally simple and model-facing. Once a real
# opener is found outside code, its contents are opaque until the closing tag.
_SILENT_OPEN_RE = re.compile(r"<silent\b[^>]*>", re.IGNORECASE)
_SILENT_CLOSE_RE = re.compile(r"</silent\s*>", re.IGNORECASE)
_RAW_HTML_OPEN_TAG_RE = re.compile(
    r"""<[A-Za-z][A-Za-z0-9-]*"""
    r"""(?:[ \t]+[A-Za-z_:][A-Za-z0-9_.:-]*"""
    r"""(?:[ \t]*=[ \t]*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*"""
    r"""[ \t]*/?>"""
)
_RAW_HTML_CLOSE_TAG_RE = re.compile(
    r"</[A-Za-z][A-Za-z0-9-]*[ \t]*>"
)

# Dynamic secret-ask markers: ``$<openAiKey>`` (case-preserving shell name). Matched only
# outside fenced/inline code so a marker shown in an example isn't treated as a real
# request — code spans are masked first.
_SECRET_REQUEST_RE = re.compile(r"\$<([A-Za-z_][A-Za-z0-9_]*)>")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def process_reply(
    text: str, *, include_quick_replies: bool = True, keep_file_links: bool = False
) -> EnhancedReply:
    """Parse *text* and return an ``EnhancedReply``.

    The returned ``.text`` has file-link markup converted to plain labels and
    the trailing button block stripped when quick replies are enabled.

    When *keep_file_links* is True the ``file://`` markdown is left intact in the
    returned text (the links are still reported in ``.files``). The avibe
    workbench needs the links in place so it can rewrite them to media-proxy URLs
    for inline rendering; IM keeps the default (links stripped to plain labels and
    uploaded to the platform separately).
    """
    text = strip_silent_blocks(text)
    secret_requests = _extract_secret_requests(text)
    files = _extract_file_links(text)
    text_no_files = text if keep_file_links else (_strip_file_links(text) if files else text)
    if include_quick_replies:
        buttons, text_clean = _extract_buttons(text_no_files)
    else:
        buttons, text_clean = [], text_no_files
    return EnhancedReply(text=text_clean.rstrip(), files=files, buttons=buttons, secret_requests=secret_requests)


def strip_file_links(text: str) -> str:
    """Remove ``file://`` markdown URLs while preserving the surrounding text."""
    files = _extract_file_links(text)
    if not files:
        return text
    return _strip_file_links(text)


def strip_silent_blocks(text: str) -> str:
    """Remove silent directives outside Markdown code spans and fences."""
    if not text:
        return text
    if "<silent" not in text.lower():
        return text

    changed = False
    while True:
        masked = _mask_markdown_code(text)
        ranges: List[Tuple[int, int]] = []
        search_from = 0

        while opener := _SILENT_OPEN_RE.search(masked, search_from):
            closing = _SILENT_CLOSE_RE.search(text, opener.end())
            if closing is None:
                ranges.append((opener.start(), len(text)))
                return _remove_ranges(text, ranges).strip()
            ranges.append((opener.start(), closing.end()))
            search_from = closing.end()

        if not ranges:
            return text.strip() if changed else text
        changed = True
        text = _remove_ranges(text, ranges)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_file_links(text: str) -> List[FileLink]:
    """Return ``FileLink`` instances found outside Markdown code."""
    results: List[FileLink] = []
    for match in _file_link_matches(text):
        bang, label, url = match.groups()
        parsed = urlparse(url)
        if parsed.scheme != "file":
            continue
        path = _file_uri_to_local_path(parsed)
        if not os.path.isabs(path):
            logger.warning("Skipping non-absolute file link: %s", url)
            continue
        results.append(FileLink(label=label, path=path, is_image=(bang == "!")))
    return results


def _file_uri_to_local_path(parsed) -> str:
    """Convert a parsed file URI into a local path for the current OS."""
    path = unquote(parsed.path)
    if os.name != "nt":
        return path

    if parsed.netloc:
        return ntpath.normpath(f"//{parsed.netloc}{path}")
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    return ntpath.normpath(path)


def _strip_file_links(text: str) -> str:
    """Replace file links outside Markdown code with their labels."""
    matches = _file_link_matches(text)
    if not matches:
        return text

    parts: List[str] = []
    cursor = 0
    for match in matches:
        parts.append(text[cursor : match.start()])
        parts.append(match.group(2))
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts)


def _file_link_matches(text: str) -> List[re.Match]:
    """Find eligible link ranges using a mask, then recover original groups."""
    matches: List[re.Match] = []
    for masked_match in _FILE_LINK_RE.finditer(_mask_markdown_code(text)):
        original_match = _FILE_LINK_RE.fullmatch(
            text,
            masked_match.start(),
            masked_match.end(),
        )
        if original_match is not None:
            matches.append(original_match)
    return matches


def _extract_secret_requests(text: str) -> List[SecretRequest]:
    """Return ordered, de-duplicated ``$<NAME>`` markers found outside code spans."""
    if not text or "$<" not in text:
        return []
    masked = _mask_markdown_code(text)
    out: List[SecretRequest] = []
    seen: set[str] = set()
    for match in _SECRET_REQUEST_RE.finditer(masked):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            out.append(SecretRequest(name=name))
    return out


def _mask_markdown_code(text: str) -> str:
    """Blank Markdown code regions without changing string offsets."""
    block_ranges = _block_code_ranges(text)
    ranges = sorted(
        [
            *block_ranges,
            *_inline_code_ranges(text, block_ranges),
        ]
    )
    if not ranges:
        return text

    parts: List[str] = []
    cursor = 0
    for start, end in ranges:
        if start < cursor:
            continue
        parts.append(text[cursor:start])
        parts.append(re.sub(r"[^\r\n]", " ", text[start:end]))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _remove_ranges(text: str, ranges: List[Tuple[int, int]]) -> str:
    """Remove sorted, non-overlapping character ranges from *text*."""
    parts: List[str] = []
    cursor = 0
    for start, end in ranges:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _block_code_ranges(text: str) -> List[Tuple[int, int]]:
    """Return CommonMark fenced and indented code block ranges."""
    line_offsets = [0]
    line_offsets.extend(
        match.end() for match in re.finditer(r"\r\n|\r|\n", text)
    )
    ranges: List[Tuple[int, int]] = []
    for token in _MARKDOWN.parse(text):
        if token.type not in {"fence", "code_block"} or token.map is None:
            continue
        start_line, end_line = token.map
        start = line_offsets[start_line]
        end = line_offsets[end_line] if end_line < len(line_offsets) else len(text)
        ranges.append((start, end))
    return ranges


def _inline_code_ranges(
    text: str,
    block_ranges: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """Return inline code spans outside fenced and indented code blocks."""
    ranges: List[Tuple[int, int]] = []
    segment_start = 0
    for block_start, block_end in block_ranges:
        ranges.extend(_inline_code_ranges_in_segment(text, segment_start, block_start))
        segment_start = block_end
    ranges.extend(_inline_code_ranges_in_segment(text, segment_start, len(text)))

    return ranges


def _inline_code_ranges_in_segment(
    text: str,
    start: int,
    end: int,
) -> List[Tuple[int, int]]:
    """Pair inline backtick runs without crossing line/block boundaries."""
    ranges: List[Tuple[int, int]] = []
    line_start = start
    while line_start < end:
        line_end = line_start
        while line_end < end and text[line_end] not in {"\r", "\n"}:
            line_end += 1
        ranges.extend(_inline_code_ranges_in_line(text, line_start, line_end))
        if line_end < end and text[line_end : line_end + 2] == "\r\n":
            line_start = line_end + 2
        else:
            line_start = line_end + 1
    return ranges


def _inline_code_ranges_in_line(
    text: str,
    start: int,
    end: int,
) -> List[Tuple[int, int]]:
    """Pair inline backtick runs in one linear pass over a single line."""
    runs: List[Tuple[int, int, int, bool]] = []
    html_ranges = _raw_html_ranges_in_line(text, start, end)
    cursor = start
    while cursor < end:
        if text[cursor] != "`":
            cursor += 1
            continue
        run_end = cursor + 1
        while run_end < end and text[run_end] == "`":
            run_end += 1
        runs.append(
            (
                cursor,
                run_end,
                run_end - cursor,
                _is_backslash_escaped(text, cursor),
            )
        )
        cursor = run_end

    next_closing: List[int | None] = [None] * len(runs)
    next_by_length: dict[int, int] = {}
    for index in range(len(runs) - 1, -1, -1):
        _, _, length, escaped = runs[index]
        opening_length = length - 1 if escaped else length
        if opening_length:
            next_closing[index] = next_by_length.get(opening_length)
        next_by_length[length] = index

    ranges: List[Tuple[int, int]] = []
    index = 0
    html_index = 0
    while index < len(runs):
        opening_start, _, _, escaped = runs[index]
        if escaped:
            opening_start += 1
        while (
            html_index < len(html_ranges)
            and html_ranges[html_index][1] <= opening_start
        ):
            html_index += 1
        inside_html = (
            html_index < len(html_ranges)
            and html_ranges[html_index][0] <= opening_start < html_ranges[html_index][1]
        )
        if (
            opening_start == runs[index][1]
            or inside_html
        ):
            index += 1
            continue
        closing_index = next_closing[index]
        if closing_index is None:
            index += 1
            continue
        ranges.append((opening_start, runs[closing_index][1]))
        index = closing_index + 1
    return ranges


def _raw_html_ranges_in_line(
    text: str,
    start: int,
    end: int,
) -> List[Tuple[int, int]]:
    """Return raw inline HTML tokens before code-span interpretation."""
    ranges: List[Tuple[int, int]] = []
    terminators = {
        "-->": _substring_positions(text, start, end, "-->"),
        "?>": _substring_positions(text, start, end, "?>"),
        "]]>": _substring_positions(text, start, end, "]]>"),
        ">": _substring_positions(text, start, end, ">"),
    }
    cursor = start
    while cursor < end:
        cursor = text.find("<", cursor, end)
        if cursor < 0:
            break

        token_end: int | None = None
        tag = _RAW_HTML_OPEN_TAG_RE.match(text, cursor, end)
        closing_tag = _RAW_HTML_CLOSE_TAG_RE.match(text, cursor, end)
        if tag is not None or closing_tag is not None:
            token_end = max(
                match.end()
                for match in (tag, closing_tag)
                if match is not None
            )
        else:
            special = _raw_html_special_end(text, cursor, end, terminators)
            if special is not None:
                token_end = special
        if token_end is None:
            cursor += 1
            continue
        ranges.append((cursor, token_end))
        cursor = token_end
    return ranges


def _substring_positions(
    text: str,
    start: int,
    end: int,
    needle: str,
) -> List[int]:
    """Return delimiter positions in one forward pass."""
    positions: List[int] = []
    cursor = start
    while (position := text.find(needle, cursor, end)) >= 0:
        positions.append(position)
        cursor = position + len(needle)
    return positions


def _raw_html_special_end(
    text: str,
    start: int,
    end: int,
    terminators: dict[str, List[int]],
) -> int | None:
    """Return a special raw HTML token end using pre-indexed terminators."""
    candidates: List[Tuple[str, str]] = []
    if text.startswith("<!--", start):
        candidates.append(("<!--", "-->"))
    if text.startswith("<?", start):
        candidates.append(("<?", "?>"))
    if text.startswith("<![CDATA[", start):
        candidates.append(("<![CDATA[", "]]>"))
    if (
        start + 2 < end
        and text.startswith("<!", start)
        and "A" <= text[start + 2] <= "Z"
    ):
        candidates.append(("<!", ">"))

    for opener, closer in candidates:
        positions = terminators[closer]
        index = bisect_left(positions, start + len(opener))
        if index < len(positions):
            return positions[index] + len(closer)
    return None


def _is_backslash_escaped(text: str, index: int) -> bool:
    """Return whether the character at *index* has an odd backslash prefix."""
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return bool(backslashes % 2)


def _extract_buttons(text: str) -> Tuple[List[QuickReplyButton], str]:
    """Extract trailing quick-reply buttons and return ``(buttons, cleaned_text)``."""
    masked_match = _BUTTON_BLOCK_RE.search(_mask_markdown_code(text))
    if masked_match is None:
        return [], text
    m = _BUTTON_BLOCK_RE.fullmatch(
        text,
        masked_match.start(),
        masked_match.end(),
    )
    if m is None:
        return [], text

    block = m.group(1)
    # A block made up solely of plain Markdown links with no ``|``/``｜``
    # separator is a genuine reference-link section (``---\n[Release notes](…)``,
    # possibly several on their own lines), not a button group — leave the text
    # untouched. Plain links become buttons only alongside a separator or another
    # unambiguous token (bare ``[label]`` / angle / Slack link).
    if _PLAIN_LINKS_ONLY_RE.fullmatch(block.strip()):
        return [], text

    buttons: List[QuickReplyButton] = []
    for bracket_label, slack_label in _BUTTON_TOKEN_RE.findall(block):
        label = bracket_label or slack_label
        label = label.strip()
        if label:
            buttons.append(QuickReplyButton(text=label))

    if not buttons:
        return [], text

    # Enforce a reasonable upper bound on button count
    buttons = buttons[:5]

    cleaned = text[: m.start()]
    return buttons, cleaned
