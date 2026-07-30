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
from markdown_it.common.html_re import HTML_TAG_RE
from markdown_it.rules_inline.autolink import AUTOLINK_RE, EMAIL_RE
from markdown_it.rules_inline.backticks import backtick as _commonmark_backtick
from markdown_it.rules_inline.state_inline import StateInline

logger = logging.getLogger(__name__)
_BLOCK_MARKDOWN = MarkdownIt("commonmark")
_INLINE_MARKDOWN = MarkdownIt("commonmark")
_INLINE_CODE_RANGES_KEY = "avibe_inline_code_ranges"
_INLINE_ANGLE_RANGES_KEY = "avibe_inline_angle_ranges"


def _track_inline_code(state: StateInline, silent: bool) -> bool:
    """Record source ranges accepted by the CommonMark backtick rule."""
    start = state.pos
    token_count = len(state.tokens)
    matched = _commonmark_backtick(state, silent)
    if (
        matched
        and not silent
        and any(token.type == "code_inline" for token in state.tokens[token_count:])
    ):
        state.env.setdefault(_INLINE_CODE_RANGES_KEY, []).append(
            (start, state.pos)
        )
    return matched


def _skip_inline_angle_token(state: StateInline, silent: bool) -> bool:
    """Skip a prevalidated raw-HTML or autolink token."""
    token_end = state.env.get(_INLINE_ANGLE_RANGES_KEY, {}).get(state.pos)
    if token_end is None or token_end > state.posMax:
        return False
    if not silent:
        state.pending += state.src[state.pos:token_end]
    state.pos = token_end
    return True


_INLINE_MARKDOWN.inline.ruler.disable(["autolink", "html_inline"])
_INLINE_MARKDOWN.inline.ruler.before(
    "backticks",
    "avibe_angle_token",
    _skip_inline_angle_token,
)
_INLINE_MARKDOWN.inline.ruler.at("backticks", _track_inline_code)

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
    r"""(?:[ \t\n\f\r]+[A-Za-z_:][A-Za-z0-9_.:-]*"""
    r"""(?:[ \t\n\f\r]*=[ \t\n\f\r]*"""
    r"""(?:[^"'=<>`\x00-\x20]+|'[^']*'|"[^"]*"))?)*"""
    r"""[ \t\n\f\r]*/?>"""
)
_RAW_HTML_CLOSE_TAG_RE = re.compile(
    r"</[A-Za-z][A-Za-z0-9-]*[ \t\n\f\r]*>"
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

    source_offsets = list(range(len(text)))
    changed = False
    while True:
        masked = _mask_markdown_code(text)
        ranges: List[Tuple[int, int]] = []
        search_from = 0

        while opener := _search_original_match(
            _SILENT_OPEN_RE,
            masked,
            source_offsets,
            search_from,
        ):
            closing = _search_original_match(
                _SILENT_CLOSE_RE,
                text,
                source_offsets,
                opener.end(),
            )
            if closing is None:
                ranges.append((opener.start(), len(text)))
                return _trim_blank_boundary_lines(_remove_ranges(text, ranges))
            ranges.append((opener.start(), closing.end()))
            search_from = closing.end()

        if not ranges:
            return _trim_blank_boundary_lines(text) if changed else text
        changed = True
        text = _remove_ranges(text, ranges)
        source_offsets = _remove_offset_ranges(source_offsets, ranges)


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
    if not any(marker in text for marker in ("`", "~~~", "    ", "\t")):
        return text
    block_ranges, inline_source_ranges = _markdown_block_ranges(text)
    ranges = sorted(
        [
            *block_ranges,
            *_inline_code_ranges(text, inline_source_ranges),
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


def _remove_offset_ranges(
    offsets: List[int],
    ranges: List[Tuple[int, int]],
) -> List[int]:
    """Remove the same ranges from a source-offset map."""
    result: List[int] = []
    cursor = 0
    for start, end in ranges:
        result.extend(offsets[cursor:start])
        cursor = end
    result.extend(offsets[cursor:])
    return result


def _search_original_match(
    pattern: re.Pattern,
    text: str,
    source_offsets: List[int],
    start: int,
) -> re.Match | None:
    """Find a directive token that was contiguous in the original source."""
    while match := pattern.search(text, start):
        if (
            source_offsets[match.end() - 1] - source_offsets[match.start()]
            == match.end() - match.start() - 1
        ):
            return match
        start = match.start() + 1
    return None


def _trim_blank_boundary_lines(text: str) -> str:
    """Remove blank boundary lines without stripping content indentation."""
    lines = text.splitlines(keepends=True)
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip(" \t\r\n"):
        start += 1
    while end > start and not lines[end - 1].strip(" \t\r\n"):
        end -= 1
    return "".join(lines[start:end]).rstrip("\r\n")


def _markdown_block_ranges(
    text: str,
) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int, str]]]:
    """Return code-block ranges and source ranges eligible for inline parsing."""
    line_offsets = [0]
    line_offsets.extend(
        match.end() for match in re.finditer(r"\r\n|\r|\n", text)
    )
    code_ranges: List[Tuple[int, int]] = []
    inline_ranges: List[Tuple[int, int, str]] = []
    for token in _BLOCK_MARKDOWN.parse(text):
        if token.map is None:
            continue
        start_line, end_line = token.map
        start = line_offsets[start_line]
        end = line_offsets[end_line] if end_line < len(line_offsets) else len(text)
        if token.type in {"fence", "code_block"}:
            code_ranges.append((start, end))
        elif token.type == "inline":
            inline_ranges.append((start, end, token.content))
    return code_ranges, inline_ranges


def _inline_code_ranges(
    text: str,
    inline_source_ranges: List[Tuple[int, int, str]],
) -> List[Tuple[int, int]]:
    """Return inline code spans from CommonMark inline-capable blocks."""
    ranges: List[Tuple[int, int]] = []
    for source_start, source_end, content in inline_source_ranges:
        ranges.extend(
            _inline_code_ranges_in_block(
                text,
                source_start,
                source_end,
                content,
            )
        )
    return ranges


def _inline_code_ranges_in_block(
    text: str,
    start: int,
    end: int,
    content: str,
) -> List[Tuple[int, int]]:
    """Map CommonMark inline-code ranges back to the original source."""
    if start >= end or not content:
        return []
    env: dict = {
        _INLINE_ANGLE_RANGES_KEY: dict(_inline_angle_token_ranges(content)),
    }
    _INLINE_MARKDOWN.inline.parse(
        content,
        _INLINE_MARKDOWN,
        env,
        [],
    )
    backtick_offsets = _inline_backtick_source_offsets(
        text,
        start,
        end,
        content,
    )
    ranges: List[Tuple[int, int]] = []
    for range_start, range_end in env.get(_INLINE_CODE_RANGES_KEY, []):
        source_start = backtick_offsets.get(range_start)
        source_last = backtick_offsets.get(range_end - 1)
        if source_start is not None and source_last is not None:
            ranges.append((source_start, source_last + 1))
    return ranges


def _inline_backtick_source_offsets(
    text: str,
    start: int,
    end: int,
    content: str,
) -> dict[int, int]:
    """Map backticks in container-stripped inline content to source offsets."""
    source_lines: List[Tuple[int, str]] = []
    line_start = start
    for match in re.finditer(r"\r\n|\r|\n", text[start:end]):
        line_end = start + match.start()
        source_lines.append((line_start, text[line_start:line_end]))
        line_start = start + match.end()
    if line_start <= end:
        source_lines.append((line_start, text[line_start:end]))

    offsets: dict[int, int] = {}
    source_index = 0
    content_offset = 0
    for content_line in content.split("\n"):
        if "`" in content_line:
            for candidate_index in range(source_index, len(source_lines)):
                absolute_start, source_line = source_lines[candidate_index]
                normalized_source_line = source_line.replace("\x00", "\ufffd")
                matched_start = normalized_source_line.find(content_line)
                relative_start = matched_start if matched_start >= 0 else None
                if relative_start is None:
                    first_backtick = content_line.find("`")
                    source_backtick = normalized_source_line.find("`")
                    if (
                        source_backtick >= 0
                        and normalized_source_line[source_backtick:].startswith(
                            content_line[first_backtick:]
                        )
                    ):
                        relative_start = source_backtick - first_backtick
                if relative_start is None:
                    continue
                for relative_offset, char in enumerate(content_line):
                    if char == "`":
                        offsets[content_offset + relative_offset] = (
                            absolute_start + relative_start + relative_offset
                        )
                source_index = candidate_index + 1
                break
        elif source_index < len(source_lines):
            source_index += 1
        content_offset += len(content_line) + 1
    return offsets


def _inline_angle_token_ranges(text: str) -> List[Tuple[int, int]]:
    """Return raw-HTML and autolink ranges without repeated suffix scans."""
    ranges: List[Tuple[int, int]] = []
    terminators = {
        "-->": _substring_positions(text, "-->"),
        "?>": _substring_positions(text, "?>"),
        "]]>": _substring_positions(text, "]]>"),
        ">": _substring_positions(text, ">"),
    }
    cursor = 0
    while (start := text.find("<", cursor)) >= 0:
        token_end = _autolink_end(text, start, terminators[">"])
        if token_end is None:
            token_end = _raw_html_end(text, start, terminators)
        if token_end is None:
            cursor = start + 1
            continue
        ranges.append((start, token_end))
        cursor = token_end
    return ranges


def _substring_positions(text: str, needle: str) -> List[int]:
    """Return delimiter positions in one forward pass."""
    positions: List[int] = []
    cursor = 0
    while (position := text.find(needle, cursor)) >= 0:
        positions.append(position)
        cursor = position + len(needle)
    return positions


def _next_position(positions: List[int], minimum: int) -> int | None:
    """Return the first indexed delimiter at or after *minimum*."""
    index = bisect_left(positions, minimum)
    return positions[index] if index < len(positions) else None


def _autolink_end(
    text: str,
    start: int,
    closing_angles: List[int],
) -> int | None:
    """Return the end of a valid CommonMark URI or email autolink."""
    closing = _next_position(closing_angles, start + 1)
    if closing is None or text.find("<", start + 1, closing) >= 0:
        return None
    value = text[start + 1:closing]
    if AUTOLINK_RE.match(value):
        normalized = _INLINE_MARKDOWN.normalizeLink(value)
    elif EMAIL_RE.match(value):
        normalized = _INLINE_MARKDOWN.normalizeLink("mailto:" + value)
    else:
        return None
    return closing + 1 if _INLINE_MARKDOWN.validateLink(normalized) else None


def _raw_html_end(
    text: str,
    start: int,
    terminators: dict[str, List[int]],
) -> int | None:
    """Return the end of a valid CommonMark inline raw-HTML token."""
    tag = _RAW_HTML_OPEN_TAG_RE.match(text, start)
    closing_tag = _RAW_HTML_CLOSE_TAG_RE.match(text, start)
    tag_ends = [
        match.end()
        for match in (tag, closing_tag)
        if match is not None
    ]
    if tag_ends:
        return max(tag_ends)

    closer = None
    if text.startswith("<!-->", start):
        closer = start + 5
    elif text.startswith("<!--->", start):
        closer = start + 6
    elif text.startswith("<!--", start):
        position = _next_position(terminators["-->"], start + 4)
        closer = position + 3 if position is not None else None
    elif text.startswith("<?", start):
        position = _next_position(terminators["?>"], start + 2)
        closer = position + 2 if position is not None else None
    elif text.startswith("<![CDATA[", start):
        position = _next_position(terminators["]]>"], start + 9)
        closer = position + 3 if position is not None else None
    elif (
        start + 2 < len(text)
        and text.startswith("<!", start)
        and text[start + 2].isalpha()
    ):
        position = _next_position(terminators[">"], start + 3)
        closer = position + 1 if position is not None else None

    if closer is None:
        return None
    match = HTML_TAG_RE.match(text[start:closer])
    return closer if match is not None and match.end() == closer - start else None


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
