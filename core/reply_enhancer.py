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
_INLINE_SILENT_RANGES_KEY = "avibe_inline_silent_ranges"


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


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches markdown links with file:// URLs, including image links. CommonMark
# permits angle brackets around destinations so paths containing spaces do not
# need escaping:
#   [label](file:///path)
#   [label](<file:///path with spaces>)
#   ![alt](<file:///path/(v1).png>)
# The lookahead validates the complete destination form before the shared URL
# capture consumes it, keeping malformed or half-paired brackets unmatched.
_BARE_FILE_URI = r"file://(?:[^()]+|\([^)]*\))+"
# Pointy destinations may contain spaces and unbalanced parentheses. Each
# repetition begins with either a non-backslash or a backslash, keeping
# malformed input linear while allowing escaped angle brackets.
_ANGLE_FILE_URI = r"file://(?:[^\\<>\r\n]|\\[^\r\n])+"
_FILE_LINK_RE = re.compile(
    rf"(!?)\[([^\]]*)\]\("
    rf"(?=(?:<{_ANGLE_FILE_URI}>|{_BARE_FILE_URI})\))"
    rf"(?:<)?((?<=<){_ANGLE_FILE_URI}|{_BARE_FILE_URI})(?:>)?\)"
)

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
        buttons, text_clean = _extract_buttons(text_no_files, mask_no_files)
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
    # CommonMark pointy destinations permit backslash-escaped angle brackets;
    # those escapes are Markdown syntax, not part of the local filename.
    path = unquote(re.sub(r"\\([<>])", r"\1", parsed.path))
    if os.name != "nt":
        return path

    if parsed.netloc:
        return ntpath.normpath(f"//{parsed.netloc}{path}")
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    return ntpath.normpath(path)


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
    """Replace eligible file links while preserving Markdown code regions."""
    matches = _file_link_matches(text)
    if not matches:
        return text
    parts: List[str] = []
    cursor = 0
    for match in matches:
        parts.append(text[cursor : match.start()])
        parts.append(replacement(match))
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts)


def _file_link_matches(
    text: str,
    markdown_mask: str | None = None,
) -> List[re.Match]:
    """Find eligible link ranges using a mask, then recover original groups."""
    matches: List[re.Match] = []
    mask = markdown_mask if markdown_mask is not None else _mask_markdown_code(text)
    for masked_match in _FILE_LINK_RE.finditer(mask):
        original_match = _FILE_LINK_RE.fullmatch(
            text,
            masked_match.start(),
            masked_match.end(),
        )
        if original_match is None:
            continue
        bracket_start = original_match.start() + (
            1 if original_match.group(1) else 0
        )
        backslash_count = 0
        cursor = bracket_start - 1
        while cursor >= 0 and text[cursor] == "\\":
            backslash_count += 1
            cursor -= 1
        if backslash_count % 2:
            continue
        matches.append(original_match)
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
) -> Tuple[List[QuickReplyButton], str]:
    """Extract trailing quick-reply buttons and return ``(buttons, cleaned_text)``."""
    mask = markdown_mask if markdown_mask is not None else _mask_markdown_code(text)
    masked_match = _BUTTON_BLOCK_RE.search(mask)
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
