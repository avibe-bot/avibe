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
from dataclasses import dataclass, field
from typing import List, Tuple
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

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
    fenced_ranges = _fenced_code_ranges(text)
    ranges = sorted([*fenced_ranges, *_inline_code_ranges(text, fenced_ranges)])
    if not ranges:
        return text

    parts: List[str] = []
    cursor = 0
    for start, end in ranges:
        parts.append(text[cursor:start])
        parts.append(" " * (end - start))
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


def _fenced_code_ranges(text: str) -> List[Tuple[int, int]]:
    """Return CommonMark-style backtick and tilde fence ranges."""
    ranges: List[Tuple[int, int]] = []
    active: Tuple[int, str, int, int, int | None] | None = None
    list_quote_depth: int | None = None
    list_indents: List[int] = []
    paragraph_open = False
    offset = 0

    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if active is None:
            (
                delimiter,
                list_quote_depth,
                list_indents,
                paragraph_open,
            ) = _opening_fence_delimiter(
                content,
                list_quote_depth,
                list_indents,
                paragraph_open,
            )
            active = _new_fence_state(offset, delimiter)
        else:
            start, marker, opening_length, quote_depth, list_indent = active
            if _line_leaves_fence_container(content, quote_depth, list_indent):
                ranges.append((start, offset))
                (
                    delimiter,
                    list_quote_depth,
                    list_indents,
                    paragraph_open,
                ) = _opening_fence_delimiter(
                    content,
                    list_quote_depth,
                    list_indents,
                    paragraph_open,
                )
                active = _new_fence_state(offset, delimiter)
            else:
                closing = _closing_fence_delimiter(content, quote_depth, list_indent)
                if closing is None:
                    offset += len(line)
                    continue
                closing_marker, closing_length, remainder = closing
                if (
                    closing_marker == marker
                    and closing_length >= opening_length
                    and not remainder.strip(" \t")
                ):
                    ranges.append((start, offset + len(line)))
                    active = None
        offset += len(line)

    if active is not None:
        ranges.append((active[0], len(text)))
    return ranges


def _new_fence_state(
    offset: int,
    delimiter: Tuple[str, int, str, int, int | None] | None,
) -> Tuple[int, str, int, int, int | None] | None:
    """Build active fence state when an eligible delimiter is an opener."""
    if delimiter is None:
        return None
    marker, length, remainder, quote_depth, list_indent = delimiter
    # A backtick fence cannot contain another backtick in its info string.
    if marker == "`" and "`" in remainder:
        return None
    return offset, marker, length, quote_depth, list_indent


def _opening_fence_delimiter(
    line: str,
    prior_quote_depth: int | None,
    prior_list_indents: List[int],
    paragraph_open: bool,
) -> Tuple[
    Tuple[str, int, str, int, int | None] | None,
    int,
    List[int],
    bool,
]:
    """Parse a fence opener while carrying list context across lines."""
    quote_depth, prefix_end = _quote_prefix(line)
    list_indents = (
        list(prior_list_indents)
        if prior_quote_depth == quote_depth
        else []
    )
    if not line[prefix_end:].strip():
        return None, quote_depth, list_indents, False

    cursor = prefix_end
    column = 0
    quote_after_list = False
    saw_list_marker = False

    while True:
        cursor, column = _consume_indentation(line, cursor, column)
        while list_indents and column < list_indents[-1]:
            list_indents.pop()

        marker = _list_marker(line, cursor)
        if marker is None:
            if list_indents and cursor < len(line) and line[cursor] == ">":
                quote_depth += 1
                cursor += 1
                if cursor < len(line) and line[cursor] in {" ", "\t"}:
                    cursor += 1
                column = 0
                list_indents = []
                quote_after_list = True
                continue
            break
        marker_end, ordered_start = marker

        minimum_column = list_indents[-1] if list_indents else 0
        maximum_column = minimum_column + 3 if list_indents else 3
        if column < minimum_column or column > maximum_column:
            break
        if (
            paragraph_open
            and not list_indents
            and ordered_start not in {None, 1}
        ):
            break

        marker_start = cursor
        marker_column = column
        column += marker_end - marker_start
        cursor = marker_end
        whitespace_start = cursor
        padding_start_column = column
        cursor, column = _consume_indentation(line, cursor, column)
        padding = column - padding_start_column
        if cursor == whitespace_start and cursor == len(line):
            if paragraph_open and not list_indents:
                cursor = marker_start
                column = marker_column
                break
            column += 1
            list_indents.append(column)
            saw_list_marker = True
            break
        if cursor == whitespace_start or padding > 4:
            cursor = marker_start
            column = marker_column
            break
        list_indents.append(column)
        saw_list_marker = True
        quote_after_list = False

    delimiter = _fence_run(line, cursor)
    if delimiter is None:
        indented_code = not list_indents and column >= 4
        return (
            None,
            quote_depth,
            list_indents,
            False
            if saw_list_marker or indented_code
            else _line_opens_paragraph(line[cursor:]),
        )

    minimum_column = (
        0
        if quote_after_list
        else (list_indents[-1] if list_indents else 0)
    )
    maximum_column = (
        3
        if quote_after_list
        else (minimum_column + 3 if list_indents else 3)
    )
    if column < minimum_column or column > maximum_column:
        return None, quote_depth, list_indents, paragraph_open

    marker, length, remainder = delimiter
    list_indent = (
        None
        if quote_after_list
        else (list_indents[-1] if list_indents else None)
    )
    return (
        (marker, length, remainder, quote_depth, list_indent),
        quote_depth,
        list_indents,
        False,
    )


def _closing_fence_delimiter(
    line: str,
    quote_depth: int,
    list_indent: int | None,
) -> Tuple[str, int, str] | None:
    """Parse a closing fence that remains inside the opening container."""
    prefix_end = _required_quote_prefix(line, quote_depth)
    if prefix_end is None:
        return None

    cursor, indent = _consume_indentation(line, prefix_end, 0)
    minimum_indent = list_indent or 0
    if indent < minimum_indent or indent > minimum_indent + 3:
        return None
    return _fence_run(line, cursor)


def _fence_run(line: str, start: int) -> Tuple[str, int, str] | None:
    """Parse a backtick or tilde fence run at *start*."""
    if start == len(line):
        return None
    marker = line[start]
    if marker not in {"`", "~"}:
        return None
    end = start
    while end < len(line) and line[end] == marker:
        end += 1
    length = end - start
    if length < 3:
        return None
    return marker, length, line[end:]


def _quote_prefix(line: str) -> Tuple[int, int]:
    """Return the block quote depth and end of its Markdown container prefix."""
    depth = 0
    cursor = 0
    while True:
        prefix_start = cursor
        spaces = 0
        while cursor < len(line) and line[cursor] == " " and spaces < 4:
            cursor += 1
            spaces += 1
        if spaces > 3 or cursor == len(line) or line[cursor] != ">":
            return depth, prefix_start
        depth += 1
        cursor += 1
        if cursor < len(line) and line[cursor] in {" ", "\t"}:
            cursor += 1


def _required_quote_prefix(line: str, quote_depth: int) -> int | None:
    """Consume exactly the quote container depth established by an opener."""
    cursor = 0
    for _ in range(quote_depth):
        spaces = 0
        while cursor < len(line) and line[cursor] == " " and spaces < 4:
            cursor += 1
            spaces += 1
        if spaces > 3 or cursor == len(line) or line[cursor] != ">":
            return None
        cursor += 1
        if cursor < len(line) and line[cursor] in {" ", "\t"}:
            cursor += 1
    return cursor


def _list_marker(line: str, start: int) -> Tuple[int, int | None] | None:
    """Return a CommonMark list marker's end and optional ordered start."""
    if start >= len(line):
        return None
    if line[start] in {"-", "+", "*"}:
        return start + 1, None

    cursor = start
    while cursor < len(line) and line[cursor].isdigit() and cursor - start < 9:
        cursor += 1
    if cursor == start or cursor == len(line) or line[cursor] not in {".", ")"}:
        return None
    return cursor + 1, int(line[start:cursor])


def _consume_indentation(
    line: str,
    start: int,
    column: int,
) -> Tuple[int, int]:
    """Consume spaces/tabs, expanding tabs to four-column Markdown stops."""
    cursor = start
    while cursor < len(line) and line[cursor] in {" ", "\t"}:
        if line[cursor] == "\t":
            column += 4 - (column % 4)
        else:
            column += 1
        cursor += 1
    return cursor, column


def _line_leaves_fence_container(
    line: str,
    quote_depth: int,
    list_indent: int | None,
) -> bool:
    """Return whether a nonblank line exits the opening fence container."""
    if not line.strip():
        return quote_depth > 0 and _required_quote_prefix(line, quote_depth) is None
    prefix_end = _required_quote_prefix(line, quote_depth)
    if prefix_end is None:
        return True
    if list_indent is None:
        return False

    _, indent = _consume_indentation(line, prefix_end, 0)
    return indent < list_indent


def _inline_code_ranges(
    text: str,
    fenced_ranges: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """Return inline code spans outside fenced code blocks."""
    ranges: List[Tuple[int, int]] = []
    segment_start = 0
    for fence_start, fence_end in fenced_ranges:
        ranges.extend(_inline_code_ranges_in_segment(text, segment_start, fence_start))
        segment_start = fence_end
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
    cursor = start
    while cursor < end:
        if text[cursor] == "<":
            html_end = _raw_html_token_end(text, cursor, end)
            if html_end is not None:
                cursor = html_end
                continue
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
    while index < len(runs):
        opening_start, _, _, escaped = runs[index]
        if escaped:
            opening_start += 1
        if opening_start == runs[index][1]:
            index += 1
            continue
        closing_index = next_closing[index]
        if closing_index is None:
            index += 1
            continue
        ranges.append((opening_start, runs[closing_index][1]))
        index = closing_index + 1
    return ranges


def _raw_html_token_end(text: str, start: int, end: int) -> int | None:
    """Return the end of a raw inline HTML token beginning at *start*."""
    cursor = start + 1
    if cursor >= end:
        return None
    if text[cursor] == "/":
        cursor += 1
    if cursor >= end or not (text[cursor].isalpha() or text[cursor] in {"!", "?"}):
        return None

    quote: str | None = None
    while cursor < end:
        char = text[cursor]
        if quote is not None:
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char == ">":
            return cursor + 1
        cursor += 1
    return None


def _line_opens_paragraph(content: str) -> bool:
    """Return whether a non-container Markdown line starts paragraph text."""
    stripped = content.lstrip(" \t")
    if re.match(r"#{1,6}(?:[ \t]+|$)", stripped):
        return False
    if re.fullmatch(r"(?:={3,}|-{3,})[ \t]*", stripped):
        return False
    compact = re.sub(r"[ \t]", "", stripped)
    if len(compact) >= 3 and len(set(compact)) == 1 and compact[0] in {"*", "-", "_"}:
        return False
    return True


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
