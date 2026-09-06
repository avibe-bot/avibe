"""Reply parser for silent blocks, file attachments, and quick-reply buttons.

Extracts special syntaxes from agent reply text:

1. **Silent blocks** – ``<silent>...</silent>`` sections that are never forwarded
   to the IM user. If nothing remains after stripping them, no message is sent.

2. **File links** – Markdown links whose URL starts with ``file://``
   e.g. ``[screenshot](file:///tmp/shot.png)``

3. **Quick-reply buttons** – A trailing row of ``[button text]`` tokens
   separated by ``|``. The preferred form starts with a ``---`` separator;
   pipe-separated rows are also accepted without it for compatibility.
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
from markdown_it.common.normalize_url import validateLink as _validate_markdown_link
from markdown_it.common.utils import isStrSpace, unescapeAll
from markdown_it.common.html_re import HTML_TAG_RE
from markdown_it.rules_inline.autolink import AUTOLINK_RE, EMAIL_RE
from markdown_it.rules_inline.backticks import backtick as _commonmark_backtick
from markdown_it.rules_inline.image import image as _commonmark_image
from markdown_it.rules_inline.link import link as _commonmark_link
from markdown_it.rules_inline.state_inline import StateInline

logger = logging.getLogger(__name__)
# Block consumers use source maps/content/levels, never parsed inline children.
_BLOCK_MARKDOWN = MarkdownIt("commonmark").disable("inline")
_INLINE_MARKDOWN = MarkdownIt("commonmark")
_INLINE_CODE_RANGES_KEY = "avibe_inline_code_ranges"
_INLINE_ANGLE_RANGES_KEY = "avibe_inline_angle_ranges"
_INLINE_SILENT_RANGES_KEY = "avibe_inline_silent_ranges"
_FILE_LINK_CAPTURES_KEY = "avibe_file_link_captures"


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


def _skip_silent_control(state: StateInline, silent: bool) -> bool:
    """Keep real control contents from influencing inline Markdown parsing."""
    token_end = state.env.get(_INLINE_SILENT_RANGES_KEY, {}).get(state.pos)
    if token_end is None or token_end > state.posMax:
        return False
    if not silent:
        state.pending += state.src[state.pos:token_end]
    state.pos = token_end
    return True


def _validate_file_link_locally(url: str) -> bool:
    """Allow file links only in the reply parser's isolated MarkdownIt."""
    return url.strip().casefold().startswith("file:") or _validate_markdown_link(url)


def _capture_file_link_rule(rule, *, is_image: bool):
    """Wrap a CommonMark rule and record its accepted source ownership."""

    def capture(state: StateInline, silent: bool) -> bool:
        start = state.pos
        token_start = len(state.tokens)
        matched = rule(state, silent)
        if not matched or silent:
            return matched

        href = None
        for token in state.tokens[token_start:]:
            if is_image and token.type == "image":
                href = token.attrGet("src")
                break
            if not is_image and token.type == "link_open":
                href = token.attrGet("href")
                break
        if not isinstance(href, str) or not href.casefold().startswith("file:"):
            return matched

        bracket_start = start + (1 if is_image else 0)
        label_start = bracket_start + 1
        label_end = state.md.helpers.parseLinkLabel(
            state,
            bracket_start,
            not is_image,
        )
        pos = label_end + 1
        if label_end < 0 or pos >= state.posMax or state.src[pos] != "(":
            return matched
        pos += 1
        while pos < state.posMax:
            char = state.src[pos]
            if not isStrSpace(char) and char != "\n":
                break
            pos += 1
        destination_start = pos
        destination = state.md.helpers.parseLinkDestination(
            state.src,
            destination_start,
            state.posMax,
        )
        if not destination.ok:
            return matched
        destination_end = destination.pos
        if state.src[destination_start : destination_start + 1] == "<":
            destination_start += 1
            destination_end -= 1
        state.env.setdefault(_FILE_LINK_CAPTURES_KEY, []).append(
            (
                start,
                state.pos,
                is_image,
                label_start,
                label_end,
                destination_start,
                destination_end,
            )
        )
        return matched

    return capture


_INLINE_MARKDOWN.inline.ruler.disable(["autolink", "html_inline"])
_INLINE_MARKDOWN.inline.ruler.before(
    "backticks",
    "avibe_angle_token",
    _skip_inline_angle_token,
)
_INLINE_MARKDOWN.inline.ruler.before(
    "avibe_angle_token",
    "avibe_silent_control",
    _skip_silent_control,
)
_INLINE_MARKDOWN.inline.ruler.at("backticks", _track_inline_code)

_FILE_LINK_MARKDOWN = MarkdownIt("commonmark")
_FILE_LINK_MARKDOWN.validateLink = _validate_file_link_locally
_FILE_LINK_MARKDOWN.inline.ruler.at(
    "link",
    _capture_file_link_rule(_commonmark_link, is_image=False),
)
_FILE_LINK_MARKDOWN.inline.ruler.at(
    "image",
    _capture_file_link_rule(_commonmark_image, is_image=True),
)

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
    visible_text: str = ""  # Source text after silent controls are removed
    files: List[FileLink] = field(default_factory=list)
    buttons: List[QuickReplyButton] = field(default_factory=list)
    secret_requests: List[SecretRequest] = field(default_factory=list)


@dataclass(frozen=True)
class _FileLinkMatch:
    """Small match-compatible value shared by the regex and angle scanner."""

    source: str
    start_offset: int
    end_offset: int
    bang: str
    label: str
    url: str
    label_start_offset: int
    label_end_offset: int
    url_start_offset: int
    url_end_offset: int

    def start(self, group: int | None = None) -> int:
        if group in (None, 0):
            return self.start_offset
        if group == 1:
            return self.start_offset
        if group == 2:
            return self.label_start_offset
        if group == 3:
            return self.url_start_offset
        raise IndexError(group)

    def end(self, group: int | None = None) -> int:
        if group in (None, 0):
            return self.end_offset
        if group == 1:
            return self.start_offset + len(self.bang)
        if group == 2:
            return self.label_end_offset
        if group == 3:
            return self.url_end_offset
        raise IndexError(group)

    def group(self, *groups: int) -> str | tuple[str, ...]:
        values = (self.source, self.bang, self.label, self.url)
        if not groups:
            return self.source
        selected = tuple(values[group] for group in groups)
        return selected[0] if len(selected) == 1 else selected

    def groups(self) -> tuple[str, str, str]:
        return self.bang, self.label, self.url


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

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

# Compatibility form for replies that omit the ``---`` line. Keep this more
# restrictive than the preferred form: it must be one trailing line with at
# least two tokens and an explicit pipe, so ordinary ``[bracketed text]`` and
# reference links remain message content.
_BUTTON_LINE_TOKEN = (
    r"(?:\[[^\]\r\n]+\]"
    + _LINK_SUFFIX
    + r"|<https?://[^|>\r\n]+\|[^>\r\n]+>)"
)
_UNSEPARATED_BUTTON_ROW_RE = re.compile(
    r"\r?\n"
    r"([ \t]*"
    + _BUTTON_LINE_TOKEN
    + r"[ \t]*(?:[|｜][ \t]*"
    + _BUTTON_LINE_TOKEN
    + r"[ \t]*)+(?:[|｜][ \t]*)?)"
    r"(?:\r?\n[ \t]*)*$"
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
_LINK_ONLY_BUTTON_ROW_RE = re.compile(
    r"\s*(?:\[[^\]\r\n]+\]\((?:<https?://[^>\r\n]+>|"
    + _PLAIN_URL
    + r")\)|<https?://[^|>\r\n]+\|[^>\r\n]+>)\s*"
    r"(?:[|｜]\s*(?:\[[^\]\r\n]+\]\((?:<https?://[^>\r\n]+>|"
    + _PLAIN_URL
    + r")\)|<https?://[^|>\r\n]+\|[^>\r\n]+>)\s*)+"
)


def _is_markdown_table_delimiter_line(line: str) -> bool:
    """Return whether *line* is a Markdown table delimiter row."""
    if "|" not in line:
        return False
    cells = line.strip().strip("|").split("|")
    return len(cells) >= 2 and all(
        re.fullmatch(r"\s*:?\-+:?\s*", cell) for cell in cells
    )


def _is_markdown_table_delimiter_before(markdown_mask: str, start: int) -> bool:
    """Return whether a separator-free row belongs to the preceding table."""
    prefix = markdown_mask[:start]
    if prefix.endswith("\n"):
        return False
    lines = prefix.splitlines()
    if not lines or "|" not in lines[-1]:
        return False

    # Walk through contiguous table rows so the final row of a multi-row table
    # is not mistaken for a quick-reply footer.
    index = len(lines) - 1
    while index >= 0 and "|" in lines[index]:
        if _is_markdown_table_delimiter_line(lines[index]):
            return index > 0 and "|" in lines[index - 1]
        index -= 1
    return False


def _markdown_container_level_at(text: str, start: int) -> int:
    """Return the CommonMark container nesting level for a candidate row."""
    newline = re.match(r"\r\n|\r|\n", text[start:])
    if newline is None:
        return 0

    line_offsets = [0]
    line_offsets.extend(
        match.end() for match in re.finditer(r"\r\n|\r|\n", text)
    )
    candidate_position = start + newline.end()
    line_index = bisect_left(line_offsets, candidate_position + 1) - 1
    for token in _BLOCK_MARKDOWN.parse(text):
        if (
            token.type == "inline"
            and token.map is not None
            and token.map[0] <= line_index < token.map[1]
        ):
            return token.level
    return 0

# Silent output blocks are intentionally simple and model-facing. Once a real
# opener is found outside code, its contents are opaque until the closing tag.
_SILENT_OPEN_PREFIX_RE = re.compile(r"<silent\b", re.IGNORECASE)
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
    text: str,
    *,
    include_quick_replies: bool = True,
    allow_unseparated_quick_replies: bool = True,
    keep_file_links: bool = False,
) -> EnhancedReply:
    """Parse *text* and return an ``EnhancedReply``.

    The returned ``.text`` has file-link markup converted to plain labels and
    the trailing button block stripped when quick replies are enabled.

    When *keep_file_links* is True the ``file://`` markdown is left intact in the
    returned text (the links are still reported in ``.files``). The avibe
    workbench needs the links in place so it can rewrite them to media-proxy URLs
    for inline rendering; IM keeps the default (links stripped to plain labels and
    uploaded to the platform separately).

    ``allow_unseparated_quick_replies`` gates only the compatibility syntax;
    explicit ``---`` button blocks retain their existing behavior.
    """
    text, markdown_mask = _strip_silent_blocks_with_mask(text)
    visible_text = text
    secret_requests = _extract_secret_requests(text, markdown_mask)
    files = _extract_file_links(text, markdown_mask)
    if keep_file_links or not files:
        text_no_files = text
        mask_no_files = markdown_mask
    else:
        text_no_files, mask_no_files = _strip_file_links_with_mask(
            text,
            markdown_mask,
        )
    if include_quick_replies:
        buttons, text_clean = _extract_buttons(
            text_no_files,
            mask_no_files,
            allow_unseparated=allow_unseparated_quick_replies,
        )
    else:
        buttons, text_clean = [], text_no_files
    return EnhancedReply(
        text=text_clean.rstrip(),
        visible_text=visible_text,
        files=files,
        buttons=buttons,
        secret_requests=secret_requests,
    )


def strip_file_links(text: str) -> str:
    """Remove ``file://`` markdown URLs while preserving the surrounding text."""
    files = _extract_file_links(text)
    if not files:
        return text
    return _strip_file_links(text)


def strip_silent_blocks(text: str) -> str:
    """Remove silent directives outside Markdown code spans and fences."""
    return _strip_silent_blocks_with_mask(text)[0]


def _strip_silent_blocks_with_mask(text: str) -> Tuple[str, str]:
    """Remove controls and retain the original Markdown eligibility mask."""
    if not text:
        return text, text
    if "<silent" not in text.lower():
        return text, _mask_markdown_code(text)

    candidates = _silent_control_candidates(text)
    ranges, markdown_mask = _silent_control_ranges_and_mask(text, candidates)

    if not ranges:
        return text, markdown_mask

    cleaned = _remove_ranges(text, ranges)
    cleaned_mask = _remove_ranges(markdown_mask, ranges)
    return _trim_blank_boundary_lines_with_mask(cleaned, cleaned_mask)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_file_links(
    text: str,
    markdown_mask: str | None = None,
) -> List[FileLink]:
    """Return ``FileLink`` instances found outside Markdown code."""
    results: List[FileLink] = []
    for match in _file_link_matches(text, markdown_mask):
        bang, label, url = match.groups()
        parsed = _parse_file_uri(url)
        if parsed.scheme.casefold() != "file":
            continue
        path = _file_uri_to_local_path(parsed)
        if not os.path.isabs(path):
            logger.warning("Skipping non-absolute file link: %s", url)
            continue
        results.append(FileLink(label=label, path=path, is_image=(bang == "!")))
    return results


def _file_uri_to_local_path(parsed) -> str:
    """Convert a parsed file URI into a local path for the current OS."""
    # CommonMark pointy destinations permit backslash-escaped angle brackets;
    # those escapes are Markdown syntax, not part of the local filename.
    path = unquote(parsed.path)
    if os.name != "nt":
        return path

    if parsed.netloc:
        return ntpath.normpath(f"//{parsed.netloc}{path}")
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    return ntpath.normpath(path)


def _parse_file_uri(value: str):
    """Parse a CommonMark-normalized file destination."""
    return urlparse(value)


def _protect_file_uri_delimiters(value: str) -> str:
    """Keep escaped URI delimiters in the path before CommonMark unescaping."""
    chars: List[str] = []
    cursor = 0
    while cursor < len(value):
        if value[cursor] != "\\":
            chars.append(value[cursor])
            cursor += 1
            continue
        slash_start = cursor
        while cursor < len(value) and value[cursor] == "\\":
            cursor += 1
        slash_count = cursor - slash_start
        if cursor < len(value) and value[cursor] in "?#":
            chars.append("\\" * (slash_count - slash_count % 2))
            chars.append(
                f"%{ord(value[cursor]):02X}"
                if slash_count % 2
                else value[cursor]
            )
            cursor += 1
            continue
        chars.append("\\" * slash_count)
    return "".join(chars)


def _normalize_file_destination(value: str) -> str:
    """Apply markdown-it's strict entity and backslash normalization once."""
    return unescapeAll(_protect_file_uri_delimiters(value))


def _mask_legacy_bare_file_link_spaces(source: str) -> str:
    """Make only legacy bare ``file://`` path spaces parseable by CommonMark.

    The returned source is the same length as the input. MarkdownIt still owns
    link/image syntax and nesting; replacing the spaces only lets it evaluate
    the one historical extension that CommonMark rejects lexically.
    """
    marker = "](file://"
    if marker not in source or " " not in source:
        return source

    masked: List[str] | None = None
    search_start = 0
    while True:
        marker_start = source.find(marker, search_start)
        if marker_start < 0:
            break
        destination_start = marker_start + 2
        cursor = destination_start + len("file://")
        depth = 0
        slash_count = 0
        space_offsets: List[int] = []
        destination_end = -1
        later_marker_start = -1
        while cursor < len(source):
            if source.startswith(marker, cursor):
                later_marker_start = cursor
                break
            char = source[cursor]
            if char in "\t\r\n":
                break
            if char == "\\":
                slash_count += 1
                cursor += 1
                continue

            escaped = slash_count % 2 == 1
            slash_count = 0
            if char == " " and depth >= 0:
                space_offsets.append(cursor)
            elif char == "(" and not escaped:
                depth += 1
            elif char == ")" and not escaped:
                if depth == 0:
                    destination_end = cursor
                    break
                depth -= 1
            cursor += 1

        if later_marker_start >= 0:
            search_start = later_marker_start
            continue
        if destination_end >= 0 and space_offsets:
            title_separator = _legacy_bare_title_separator(
                source,
                destination_start,
                destination_end,
            )
            if title_separator is not None:
                space_offsets = [
                    offset for offset in space_offsets if offset < title_separator
                ]
            if not space_offsets:
                search_start = destination_end + 1
                continue
            if masked is None:
                masked = list(source)
            for offset in space_offsets:
                masked[offset] = "_"
            search_start = destination_end + 1
        elif cursor >= len(source):
            break
        else:
            search_start = cursor + 1

    return "".join(masked) if masked is not None else source


def _legacy_bare_title_separator(
    source: str,
    destination_start: int,
    destination_end: int,
) -> int | None:
    """Return the ASCII-space separator before a standard trailing title."""
    if destination_end <= destination_start + 2:
        return None
    closer = source[destination_end - 1]
    opener = "(" if closer == ")" else closer
    if opener not in "\"'(":
        return None

    cursor = destination_end - 2
    title_start = -1
    while cursor >= destination_start:
        if source[cursor] != opener:
            cursor -= 1
            continue
        slash_start = cursor
        while slash_start > destination_start and source[slash_start - 1] == "\\":
            slash_start -= 1
        if (cursor - slash_start) % 2 == 0:
            title_start = cursor
            break
        cursor = slash_start - 1
    if title_start <= destination_start or source[title_start - 1] != " ":
        return None

    title = _FILE_LINK_MARKDOWN.helpers.parseLinkTitle(
        source,
        title_start,
        destination_end,
    )
    if not title.ok or title.pos != destination_end:
        return None
    separator = title_start - 1
    while separator > destination_start and source[separator - 1] == " ":
        separator -= 1
    return separator


def _inline_file_link_captures(
    content: str,
) -> list[tuple[int, int, bool, int, int, int, int]]:
    """Capture CommonMark links plus the bounded legacy bare-space extension."""
    env: dict = {_FILE_LINK_CAPTURES_KEY: []}
    _FILE_LINK_MARKDOWN.inline.parse(content, _FILE_LINK_MARKDOWN, env, [])
    captures = list(env[_FILE_LINK_CAPTURES_KEY])

    compatibility_source = _mask_legacy_bare_file_link_spaces(content)
    if compatibility_source == content:
        return captures

    compatibility_env: dict = {_FILE_LINK_CAPTURES_KEY: []}
    _FILE_LINK_MARKDOWN.inline.parse(
        compatibility_source,
        _FILE_LINK_MARKDOWN,
        compatibility_env,
        [],
    )
    claimed_spans = {(capture[0], capture[1]) for capture in captures}
    for capture in compatibility_env[_FILE_LINK_CAPTURES_KEY]:
        destination = content[capture[5] : capture[6]]
        if (
            (capture[0], capture[1]) not in claimed_spans
            and content[capture[5] - 1 : capture[5]] != "<"
            and destination.startswith("file://")
            and " " in destination
        ):
            captures.append(capture)
    return captures


def _strip_file_links(text: str) -> str:
    """Replace file links outside Markdown code with their labels."""
    return _strip_file_links_with_mask(text, _mask_markdown_code(text))[0]


def _strip_file_links_with_mask(text: str, markdown_mask: str) -> Tuple[str, str]:
    """Replace eligible file links while keeping text and mask aligned."""
    matches = _file_link_matches(text, markdown_mask)
    if not matches:
        return text, markdown_mask

    text_parts: List[str] = []
    mask_parts: List[str] = []
    cursor = 0
    for match in matches:
        text_parts.append(text[cursor : match.start()])
        text_parts.append(match.group(2))
        mask_parts.append(markdown_mask[cursor : match.start()])
        mask_parts.append(match.group(2))
        cursor = match.end()
    text_parts.append(text[cursor:])
    mask_parts.append(markdown_mask[cursor:])
    return "".join(text_parts), "".join(mask_parts)


def _replace_file_links(text: str, replacement) -> str:
    """Replace eligible file-link destinations while preserving Markdown syntax."""
    matches = _file_link_matches(text)
    if not matches:
        return text
    parts: List[str] = []
    cursor = 0
    for match in matches:
        parts.append(text[cursor : match.start(3)])
        parts.append(replacement(match))
        cursor = match.end(3)
    parts.append(text[cursor:])
    return "".join(parts)


def _inline_source_offsets(
    text: str,
    start: int,
    end: int,
    content: str,
) -> dict[int, int]:
    """Map container-stripped inline content offsets to source offsets."""
    source_lines: List[Tuple[int, str]] = []
    line_start = start
    for match in re.finditer(r"\r\n|\r|\n", text[start:end]):
        line_end = start + match.start()
        source_lines.append((line_start, text[line_start:line_end]))
        line_start = start + match.end()
    source_lines.append((line_start, text[line_start:end]))

    offsets: dict[int, int] = {}
    source_index = 0
    content_offset = 0
    for content_line in content.split("\n"):
        for candidate_index in range(source_index, len(source_lines)):
            absolute_start, source_line = source_lines[candidate_index]
            normalized_source_line = source_line.replace("\x00", "\ufffd")
            relative_start = normalized_source_line.find(content_line)
            if relative_start < 0:
                continue
            for relative_offset in range(len(content_line) + 1):
                offsets[content_offset + relative_offset] = (
                    absolute_start + relative_start + relative_offset
                )
            source_index = candidate_index + 1
            break
        content_offset += len(content_line) + 1
    return offsets


def _map_file_link_capture(
    capture: tuple[int, int, bool, int, int, int, int],
    offsets: dict[int, int],
) -> tuple[int, int, bool, int, int, int, int] | None:
    """Translate one inline-token capture back to the original source."""
    start, end, is_image, label_start, label_end, url_start, url_end = capture
    boundaries = (start, end - 1, label_start, label_end, url_start, url_end)
    if any(boundary not in offsets for boundary in boundaries):
        return None
    return (
        offsets[start],
        offsets[end - 1] + 1,
        is_image,
        offsets[label_start],
        offsets[label_end],
        offsets[url_start],
        offsets[url_end],
    )


def _file_link_matches(
    text: str,
    markdown_mask: str | None = None,
) -> List[_FileLinkMatch]:
    """Return file links accepted by CommonMark and the local-path policy."""
    code_ranges, inline_ranges, _ = _markdown_block_ranges(text)
    captures: list[tuple[int, int, bool, int, int, int, int]] = []
    for source_start, source_end, content in inline_ranges:
        inline_captures = _inline_file_link_captures(content)
        if not inline_captures:
            continue
        offsets = _inline_source_offsets(
            text,
            source_start,
            source_end,
            content,
        )
        for capture in inline_captures:
            mapped = _map_file_link_capture(capture, offsets)
            if mapped is not None:
                captures.append(mapped)

    if markdown_mask is not None:
        for source_start, source_end in code_ranges:
            if "[" not in markdown_mask[source_start:source_end]:
                continue
            for capture in _inline_file_link_captures(
                text[source_start:source_end]
            ):
                captures.append(
                    (
                        source_start + capture[0],
                        source_start + capture[1],
                        capture[2],
                        source_start + capture[3],
                        source_start + capture[4],
                        source_start + capture[5],
                        source_start + capture[6],
                    )
                )

    candidates: List[_FileLinkMatch] = []
    captures = sorted(
        captures,
        key=lambda capture: (capture[0], -capture[1]),
    )
    for capture in captures:
        (
            start,
            end,
            is_image,
            label_start,
            label_end,
            url_start,
            url_end,
        ) = capture
        opener_end = start + (2 if is_image else 1)
        if (
            markdown_mask is not None
            and markdown_mask[start:opener_end] != text[start:opener_end]
        ):
            continue
        normalized_url = _normalize_file_destination(text[url_start:url_end])
        try:
            parsed = _parse_file_uri(normalized_url)
        except ValueError:
            continue
        path = _file_uri_to_local_path(parsed)
        if parsed.scheme.casefold() != "file" or not os.path.isabs(path):
            continue
        candidates.append(
            _FileLinkMatch(
                source=text[start:end],
                start_offset=start,
                end_offset=end,
                bang="!" if is_image else "",
                label=text[label_start:label_end],
                url=normalized_url,
                label_start_offset=label_start,
                label_end_offset=label_end,
                url_start_offset=url_start,
                url_end_offset=url_end,
            )
        )
    matches: List[_FileLinkMatch] = []
    covered_until = 0
    for candidate in candidates:
        if candidate.start() < covered_until:
            continue
        matches.append(candidate)
        covered_until = candidate.end()
    return matches


def _extract_secret_requests(
    text: str,
    markdown_mask: str | None = None,
) -> List[SecretRequest]:
    """Return ordered, de-duplicated ``$<NAME>`` markers found outside code spans."""
    if not text or "$<" not in text:
        return []
    masked = markdown_mask if markdown_mask is not None else _mask_markdown_code(text)
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
    ranges, _ = _markdown_code_ranges(text)
    if not ranges:
        return text

    return _mask_ranges(text, ranges)


def _markdown_code_ranges(
    text: str,
) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    """Return code ranges and block ranges that can suppress later parsing."""
    if not any(marker in text for marker in ("`", "~~~", "    ", "\t")):
        return [], []
    block_ranges, inline_source_ranges, blocking_ranges = (
        _markdown_block_ranges(text)
    )
    ranges = sorted(
        [*block_ranges, *_inline_code_ranges(text, inline_source_ranges)]
    )
    return ranges, blocking_ranges


def _mask_ranges(text: str, ranges: List[Tuple[int, int]]) -> str:
    """Blank ranges while preserving newlines and every source offset."""

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


def _neutralize_ranges(text: str, ranges: List[Tuple[int, int]]) -> str:
    """Replace ranges with plain text while preserving newlines and offsets."""
    parts: List[str] = []
    cursor = 0
    for start, end in ranges:
        if start < cursor:
            continue
        parts.append(text[cursor:start])
        parts.append(re.sub(r"[^\r\n]", "x", text[start:end]))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _silent_control_candidates(text: str) -> List[Tuple[int, int]]:
    """Return every lexical opener before applying Markdown eligibility."""
    candidates: List[Tuple[int, int]] = []
    cursor = 0
    for prefix in _SILENT_OPEN_PREFIX_RE.finditer(text):
        if prefix.start() < cursor:
            continue
        closing_angle = text.find(">", prefix.end())
        if closing_angle < 0:
            break
        token_end = closing_angle + 1
        candidates.append((prefix.start(), token_end))
        cursor = token_end
    return candidates


def _silent_closing_candidates(text: str) -> List[Tuple[int, int]]:
    """Return every lexical closing tag in source order."""
    return [match.span() for match in _SILENT_CLOSE_RE.finditer(text)]


def _silent_ranges_for_openers(
    text: str,
    openers: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """Pair confirmed openers while treating each real control as opaque."""
    ranges: List[Tuple[int, int]] = []
    closers = _silent_closing_candidates(text)
    closer_index = 0
    covered_until = 0
    for start, opener_end in openers:
        if start < covered_until:
            continue
        while (
            closer_index < len(closers)
            and closers[closer_index][0] < opener_end
        ):
            closer_index += 1
        has_closing = closer_index < len(closers)
        if has_closing:
            end = closers[closer_index][1]
            closer_index += 1
        else:
            end = len(text)
        ranges.append((start, end))
        covered_until = end
        if not has_closing:
            break
    return ranges


def _silent_control_ranges_and_mask(
    text: str,
    candidates: List[Tuple[int, int]],
) -> Tuple[List[Tuple[int, int]], str]:
    """Resolve controls with hidden contents opaque to block Markdown.

    A real control can start an HTML block or contain a fence that suppresses
    later code parsing. Reparse a same-length source with confirmed controls
    hidden and uncertain tags neutralized, preserving code delimiters and source
    offsets. A final batch refinement bounds adversarial unmatched-fence chains.
    """
    code_ranges, block_ranges = _markdown_code_ranges(text)
    controls, _ = _partition_ranges_by_start(candidates, code_ranges)
    parsed_controls: List[Tuple[int, int]] = []

    for _ in range(2):
        control_ranges = _silent_ranges_for_openers(text, controls)
        _, invalid_blocks = _partition_ranges_by_start(
            block_ranges,
            control_ranges,
        )
        if not invalid_blocks:
            break

        stable_controls, uncertain_controls = _partition_ranges_by_start(
            controls,
            invalid_blocks,
        )
        trigger_starts = _container_starts_for_range_starts(
            control_ranges,
            invalid_blocks,
        )
        confirmed_controls = sorted(
            [
                *stable_controls,
                *(
                    control
                    for control in uncertain_controls
                    if control[0] in trigger_starts
                ),
            ]
        )
        _, provisional_openers = _partition_ranges_by_start(
            candidates,
            invalid_blocks,
        )
        _, provisional_closers = _partition_ranges_by_start(
            _silent_closing_candidates(text),
            invalid_blocks,
        )
        confirmed_ranges = _silent_ranges_for_openers(
            text,
            confirmed_controls,
        )
        provisional_tags, _ = _partition_ranges_by_start(
            sorted({*provisional_openers, *provisional_closers}),
            confirmed_ranges,
        )
        opaque_source = _mask_ranges(text, confirmed_ranges)
        opaque_source = _neutralize_ranges(opaque_source, provisional_tags)
        code_ranges, block_ranges = _markdown_code_ranges(opaque_source)
        parsed_controls = confirmed_controls
        discovered, _ = _partition_ranges_by_start(candidates, code_ranges)
        controls = sorted({*confirmed_controls, *discovered})

    control_ranges = _silent_ranges_for_openers(text, controls)
    _, invalid_blocks = _partition_ranges_by_start(
        block_ranges,
        control_ranges,
    )
    if invalid_blocks:
        _, provisional_candidates = _partition_ranges_by_start(
            candidates,
            invalid_blocks,
        )
        provisional = sorted(
            {
                *controls,
                *provisional_candidates,
            }
        )
        provisional_ranges = _silent_ranges_for_openers(text, provisional)
        opaque_source = _mask_ranges(text, provisional_ranges)
        code_ranges, _ = _markdown_code_ranges(opaque_source)
        parsed_controls = provisional
        discovered, _ = _partition_ranges_by_start(candidates, code_ranges)
        controls = sorted({*controls, *discovered})

    if parsed_controls and parsed_controls != controls:
        control_ranges = _silent_ranges_for_openers(text, controls)
        opaque_source = _mask_ranges(text, control_ranges)
        code_ranges, _ = _markdown_code_ranges(opaque_source)
    return _silent_ranges_for_openers(text, controls), _mask_ranges(
        text,
        code_ranges,
    )


def _container_starts_for_range_starts(
    containers: List[Tuple[int, int]],
    ranges: List[Tuple[int, int]],
) -> set[int]:
    """Return starts of sorted containers covering any sorted range start."""
    starts: set[int] = set()
    container_index = 0
    for source_range in ranges:
        position = source_range[0]
        while (
            container_index < len(containers)
            and containers[container_index][1] <= position
        ):
            container_index += 1
        if (
            container_index < len(containers)
            and containers[container_index][0] <= position
            and position < containers[container_index][1]
        ):
            starts.add(containers[container_index][0])
    return starts


def _partition_ranges_by_start(
    ranges: List[Tuple[int, int]],
    containers: List[Tuple[int, int]],
) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    """Partition start-sorted ranges by start-sorted container membership."""
    outside: List[Tuple[int, int]] = []
    inside: List[Tuple[int, int]] = []
    container_index = 0
    for source_range in ranges:
        position = source_range[0]
        while (
            container_index < len(containers)
            and containers[container_index][1] <= position
        ):
            container_index += 1
        if (
            container_index < len(containers)
            and containers[container_index][0] <= position
            and position < containers[container_index][1]
        ):
            inside.append(source_range)
        else:
            outside.append(source_range)
    return outside, inside


def _remove_ranges(text: str, ranges: List[Tuple[int, int]]) -> str:
    """Remove sorted, non-overlapping character ranges from *text*."""
    parts: List[str] = []
    cursor = 0
    for start, end in ranges:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _trim_blank_boundary_lines_with_mask(text: str, mask: str) -> Tuple[str, str]:
    """Apply text-derived boundary trimming to an aligned Markdown mask."""
    lines = text.splitlines(keepends=True)
    mask_lines = mask.splitlines(keepends=True)
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip(" \t\r\n"):
        start += 1
    while end > start and not lines[end - 1].strip(" \t\r\n"):
        end -= 1
    cleaned = "".join(lines[start:end]).rstrip("\r\n")
    cleaned_mask = "".join(mask_lines[start:end])[: len(cleaned)]
    return cleaned, cleaned_mask


def _markdown_block_ranges(
    text: str,
) -> Tuple[
    List[Tuple[int, int]],
    List[Tuple[int, int, str]],
    List[Tuple[int, int]],
]:
    """Return code, inline-capable, and parser-blocking source ranges."""
    line_offsets = [0]
    line_offsets.extend(
        match.end() for match in re.finditer(r"\r\n|\r|\n", text)
    )
    code_ranges: List[Tuple[int, int]] = []
    inline_ranges: List[Tuple[int, int, str]] = []
    blocking_ranges: List[Tuple[int, int]] = []
    for token in _BLOCK_MARKDOWN.parse(text):
        if token.map is None:
            continue
        start_line, end_line = token.map
        start = line_offsets[start_line]
        end = line_offsets[end_line] if end_line < len(line_offsets) else len(text)
        if token.type in {"fence", "code_block"}:
            code_ranges.append((start, end))
            blocking_ranges.append((start, end))
        elif token.type == "html_block":
            first_line_end = (
                line_offsets[start_line + 1]
                if start_line + 1 < len(line_offsets)
                else len(text)
            )
            html_start = text.find("<", start, first_line_end)
            blocking_ranges.append(
                (html_start if html_start >= 0 else start, end)
            )
        elif token.type == "inline":
            inline_ranges.append((start, end, token.content))
    return code_ranges, inline_ranges, blocking_ranges


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
    silent_candidates = _silent_control_candidates(content)
    env[_INLINE_SILENT_RANGES_KEY] = {
        start: end
        for start, end in _silent_ranges_for_openers(
            content,
            silent_candidates,
        )
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


def _extract_buttons(
    text: str,
    markdown_mask: str | None = None,
    *,
    allow_unseparated: bool = True,
) -> Tuple[List[QuickReplyButton], str]:
    """Extract trailing quick-reply buttons and return ``(buttons, cleaned_text)``."""
    mask = markdown_mask if markdown_mask is not None else _mask_markdown_code(text)
    pattern = _BUTTON_BLOCK_RE
    masked_match = pattern.search(mask)
    if masked_match is None and allow_unseparated:
        pattern = _UNSEPARATED_BUTTON_ROW_RE
        masked_match = pattern.search(mask)
    if masked_match is None:
        return [], text
    m = pattern.fullmatch(
        text,
        masked_match.start(),
        masked_match.end(),
    )
    if m is None:
        return [], text

    if pattern is _UNSEPARATED_BUTTON_ROW_RE and not text[: m.start()].strip():
        return [], text

    if pattern is _UNSEPARATED_BUTTON_ROW_RE:
        code_ranges, _, blocking_ranges = _markdown_block_ranges(text)
        html_block_ranges = [
            source_range
            for source_range in blocking_ranges
            if source_range not in code_ranges
        ]
        if any(start <= m.start(1) < end for start, end in html_block_ranges):
            return [], text
        if _markdown_container_level_at(text, m.start()) > 1:
            return [], text

    if pattern is _UNSEPARATED_BUTTON_ROW_RE and _is_markdown_table_delimiter_before(
        mask, m.start()
    ):
        return [], text

    block = m.group(1)
    if pattern is _UNSEPARATED_BUTTON_ROW_RE and _LINK_ONLY_BUTTON_ROW_RE.fullmatch(
        block.strip()
    ):
        return [], text
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

    if pattern is _UNSEPARATED_BUTTON_ROW_RE and len(buttons) > 5:
        return [], text

    if pattern is _UNSEPARATED_BUTTON_ROW_RE and len(buttons) < 2:
        return [], text

    # Enforce a reasonable upper bound on button count
    buttons = buttons[:5]

    cleaned = text[: m.start()]
    return buttons, cleaned


def strip_quick_reply_buttons(text: str) -> str:
    """Remove a trailing quick-reply row while preserving all other markup."""
    _, cleaned = _extract_buttons(text)
    return cleaned
