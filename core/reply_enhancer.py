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

# Silent output blocks are intentionally simple and model-facing.  They are
# stripped before any reply enhancement parsing so hidden text cannot create
# file uploads or quick replies.
_SILENT_BLOCK_RE = re.compile(r"<silent\b[^>]*>.*?</silent\s*>", re.IGNORECASE | re.DOTALL)
_UNTERMINATED_SILENT_RE = re.compile(r"<silent\b[^>]*>.*\Z", re.IGNORECASE | re.DOTALL)

# Dynamic secret-ask markers: ``$<openAiKey>`` (case-preserving shell name). Matched only
# outside fenced/inline code so a marker shown in an example isn't treated as a real
# request — code spans are masked first.
_SECRET_REQUEST_RE = re.compile(r"\$<([A-Za-z_][A-Za-z0-9_]*)>")
_CODE_SPAN_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)


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

    masked = _mask_markdown_code(text)
    complete_ranges = [match.span() for match in _SILENT_BLOCK_RE.finditer(masked)]
    stripped = _remove_ranges(text, complete_ranges) if complete_ranges else text

    # Preserve the existing recovery contract: after complete blocks are gone, an
    # unmatched real directive consumes the rest of the reply. Re-scan Markdown
    # because removing a real block can also remove backticks contained inside it.
    unterminated = _UNTERMINATED_SILENT_RE.search(_mask_markdown_code(stripped))
    if unterminated:
        stripped = stripped[: unterminated.start()]

    if not complete_ranges and unterminated is None:
        return text
    return stripped.strip()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_file_links(text: str) -> List[FileLink]:
    """Return all ``FileLink`` instances found in *text*."""
    results: List[FileLink] = []
    for bang, label, url in _FILE_LINK_RE.findall(text):
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
    """Replace ``[label](file://…)`` with just the label."""

    def _replacer(m: re.Match) -> str:
        label = m.group(2)
        url = m.group(3)
        if url.startswith("file://"):
            return label  # keep the label text, drop the link
        return m.group(0)

    return _FILE_LINK_RE.sub(_replacer, text)


def _extract_secret_requests(text: str) -> List[SecretRequest]:
    """Return ordered, de-duplicated ``$<NAME>`` markers found outside code spans."""
    if not text or "$<" not in text:
        return []
    masked = _CODE_SPAN_RE.sub(lambda m: " " * len(m.group(0)), text)
    out: List[SecretRequest] = []
    seen: set[str] = set()
    for match in _SECRET_REQUEST_RE.finditer(masked):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            out.append(SecretRequest(name=name))
    return out


def _remove_ranges(text: str, ranges: List[Tuple[int, int]]) -> str:
    """Remove sorted, non-overlapping character ranges from *text*."""
    if not ranges:
        return text
    parts: List[str] = []
    cursor = 0
    for start, end in ranges:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


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


def _fenced_code_ranges(text: str) -> List[Tuple[int, int]]:
    """Return CommonMark-style backtick and tilde fence ranges."""
    ranges: List[Tuple[int, int]] = []
    active: Tuple[int, str, int] | None = None
    offset = 0

    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        delimiter = _fence_delimiter(content)
        if active is None:
            if delimiter is not None:
                marker, length, remainder = delimiter
                # A backtick fence cannot contain another backtick in its info
                # string. Tilde info strings have no equivalent restriction.
                if marker != "`" or "`" not in remainder:
                    active = (offset, marker, length)
        else:
            start, marker, opening_length = active
            if delimiter is not None:
                closing_marker, closing_length, remainder = delimiter
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


def _fence_delimiter(line: str) -> Tuple[str, int, str] | None:
    """Parse a fence delimiter after at most three leading spaces."""
    indent = 0
    while indent < len(line) and line[indent] == " ":
        indent += 1
    if indent > 3 or indent == len(line):
        return None

    marker = line[indent]
    if marker not in {"`", "~"}:
        return None
    end = indent
    while end < len(line) and line[end] == marker:
        end += 1
    length = end - indent
    if length < 3:
        return None
    return marker, length, line[end:]


def _inline_code_ranges(
    text: str,
    fenced_ranges: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """Return inline code spans outside fenced code blocks."""
    ranges: List[Tuple[int, int]] = []
    fence_index = 0
    index = 0

    while index < len(text):
        while fence_index < len(fenced_ranges) and fenced_ranges[fence_index][1] <= index:
            fence_index += 1
        if fence_index < len(fenced_ranges):
            fence_start, fence_end = fenced_ranges[fence_index]
            if fence_start <= index < fence_end:
                index = fence_end
                continue

        if text[index] != "`" or _is_backslash_escaped(text, index):
            index += 1
            continue

        opening_end = index
        while opening_end < len(text) and text[opening_end] == "`":
            opening_end += 1
        delimiter_length = opening_end - index
        search = opening_end
        closing_end: int | None = None

        while search < len(text):
            if fence_index < len(fenced_ranges):
                fence_start, _ = fenced_ranges[fence_index]
                # Inline spans do not cross a fenced block boundary.
                if search >= fence_start:
                    break
            if text[search] != "`":
                search += 1
                continue
            run_end = search
            while run_end < len(text) and text[run_end] == "`":
                run_end += 1
            if run_end - search == delimiter_length:
                closing_end = run_end
                break
            search = run_end

        if closing_end is None:
            index = opening_end
            continue
        ranges.append((index, closing_end))
        index = closing_end

    return ranges


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
    m = _BUTTON_BLOCK_RE.search(text)
    if not m:
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
