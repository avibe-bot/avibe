"""Matching and diff helpers for ``edit`` (C-7 section 4).

Ported from Pi ``packages/coding-agent/src/core/tools/edit-diff.ts`` (MIT,
Copyright (c) 2025 Mario Zechner): exact match first, then Pi's deterministic
normalized match with no similarity threshold, every edit matched against the
original, overlapping edits rejected. Avibe adds ``replaceAll`` (from Claude
Code and OpenCode): every occurrence in the tier that matched, each one a span
for the overlap check.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

BOM = "\ufeff"

# JavaScript's String.prototype.trimEnd set, which Pi uses (Python's isspace differs).
_TRAILING_SPACE = re.compile("[ \t\v\f\r\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+$")
_SMART_SINGLE = re.compile("[\u2018\u2019\u201a\u201b]")
_SMART_DOUBLE = re.compile("[\u201c\u201d\u201e\u201f]")
_DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
_SPECIAL_SPACES = re.compile("[\u00a0\u2002-\u200a\u202f\u205f\u3000]")
_LINES_WITH_ENDINGS = re.compile(r"[^\n]*\n|[^\n]+")


class EditError(ValueError):
    """An edit that cannot be applied; the message is the model-facing error."""


@dataclass(frozen=True)
class Edit:
    old_text: str
    new_text: str
    replace_all: bool = False


@dataclass(frozen=True)
class _Replacement:
    edit_index: int
    index: int
    length: int
    new_text: str


def split_bom(content: str) -> tuple[str, str]:
    return (BOM, content[1:]) if content.startswith(BOM) else ("", content)


def detect_line_ending(content: str) -> str:
    crlf = content.find("\r\n")
    lf = content.find("\n")
    if lf == -1 or crlf == -1:
        return "\n"
    return "\r\n" if crlf < lf else "\n"


def normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


def normalize_for_fuzzy_match(text: str) -> str:
    """NFKC, no trailing whitespace per line, ASCII quotes and dashes, plain spaces."""
    text = unicodedata.normalize("NFKC", text)
    text = "\n".join(_TRAILING_SPACE.sub("", line) for line in text.split("\n"))
    text = _SMART_SINGLE.sub("'", text)
    text = _SMART_DOUBLE.sub('"', text)
    text = _DASHES.sub("-", text)
    return _SPECIAL_SPACES.sub(" ", text)


def _find_all(content: str, needle: str) -> list[int]:
    found: list[int] = []
    if not needle:
        return found
    index = content.find(needle)
    while index != -1:
        found.append(index)
        index = content.find(needle, index + len(needle))
    return found


def _find(content: str, old_text: str) -> Optional[tuple[int, int, bool]]:
    """``(index, length, used_fuzzy)`` of the first match, exact before normalized."""
    exact = content.find(old_text)
    if exact != -1:
        return exact, len(old_text), False
    fuzzy_old = normalize_for_fuzzy_match(old_text)
    fuzzy = normalize_for_fuzzy_match(content).find(fuzzy_old)
    if fuzzy == -1:
        return None
    return fuzzy, len(fuzzy_old), True


def _find_every(content: str, old_text: str) -> list[tuple[int, int]]:
    """All occurrences in the first tier that has one. ``content`` is already normalized when needed."""
    exact = _find_all(content, old_text)
    if exact:
        return [(index, len(old_text)) for index in exact]
    fuzzy_old = normalize_for_fuzzy_match(old_text)
    return [(index, len(fuzzy_old)) for index in _find_all(normalize_for_fuzzy_match(content), fuzzy_old)]


def _count_occurrences(content: str, old_text: str) -> int:
    fuzzy_old = normalize_for_fuzzy_match(old_text)
    if not fuzzy_old:
        # Whitespace-only text normalizes to nothing; count it as written.
        return content.count(old_text)
    return normalize_for_fuzzy_match(content).count(fuzzy_old)


def _not_found(path: str, index: int, total: int) -> EditError:
    if total == 1:
        return EditError(
            f"Could not find the exact text in {path}. The old text must match exactly including all whitespace "
            "and newlines."
        )
    return EditError(
        f"Could not find edits[{index}] in {path}. The oldText must match exactly including all whitespace and "
        "newlines."
    )


def _duplicate(path: str, index: int, total: int, occurrences: int) -> EditError:
    if total == 1:
        return EditError(
            f"Found {occurrences} occurrences of the text in {path}. The text must be unique. Please provide more "
            "context to make it unique."
        )
    return EditError(
        f"Found {occurrences} occurrences of edits[{index}] in {path}. Each oldText must be unique. Please provide "
        "more context to make it unique."
    )


def _empty(path: str, index: int, total: int) -> EditError:
    if total == 1:
        return EditError(f"oldText must not be empty in {path}.")
    return EditError(f"edits[{index}].oldText must not be empty in {path}.")


def _no_change(path: str, total: int) -> EditError:
    if total == 1:
        return EditError(
            f"No changes made to {path}. The replacement produced identical content. This might indicate an issue "
            "with special characters or the text not existing as expected."
        )
    return EditError(f"No changes made to {path}. The replacements produced identical content.")


def _apply(content: str, replacements: list[_Replacement], offset: int = 0) -> str:
    result = content
    for replacement in reversed(replacements):
        start = replacement.index - offset
        result = result[:start] + replacement.new_text + result[start + replacement.length :]
    return result


def _line_spans(content: str) -> list[tuple[int, int]]:
    spans = []
    offset = 0
    for line in _LINES_WITH_ENDINGS.findall(content):
        spans.append((offset, offset + len(line)))
        offset += len(line)
    return spans


def _line_range(lines: list[tuple[int, int]], replacement: _Replacement) -> tuple[int, int]:
    start, end = replacement.index, replacement.index + replacement.length
    start_line = next((i for i, (lo, hi) in enumerate(lines) if lo <= start < hi), -1)
    if start_line == -1:
        raise EditError("Replacement range is outside the base content.")
    end_line = start_line
    while end_line < len(lines) and lines[end_line][1] < end:
        end_line += 1
    if end_line >= len(lines):
        raise EditError("Replacement range is outside the base content.")
    return start_line, end_line + 1


def _apply_preserving_unchanged_lines(original: str, base: str, replacements: list[_Replacement]) -> str:
    """Apply replacements found in the normalized ``base`` to ``original``.

    Lines a replacement touches are rewritten from the normalized base; every
    other line keeps its original bytes.
    """
    original_lines = _LINES_WITH_ENDINGS.findall(original)
    base_lines = _line_spans(base)
    if len(original_lines) != len(base_lines):
        raise EditError("Cannot preserve unchanged lines because the base content has a different line count.")
    groups: list[list] = []  # [start_line, end_line, replacements]
    for replacement in sorted(replacements, key=lambda r: r.index):
        start_line, end_line = _line_range(base_lines, replacement)
        if groups and start_line < groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], end_line)
            groups[-1][2].append(replacement)
            continue
        groups.append([start_line, end_line, [replacement]])
    out: list[str] = []
    line = 0
    for start_line, end_line, members in groups:
        out.append("".join(original_lines[line:start_line]))
        lo, hi = base_lines[start_line][0], base_lines[end_line - 1][1]
        out.append(_apply(base[lo:hi], members, lo))
        line = end_line
    out.append("".join(original_lines[line:]))
    return "".join(out)


def apply_edits_to_normalized_content(content: str, edits: list[Edit], path: str) -> str:
    """Apply every edit to LF-normalized ``content`` and return the new content.

    If any edit needs the normalized match, all edits are matched in normalized
    space and only the touched lines are taken from it.
    """
    edits = [Edit(normalize_to_lf(e.old_text), normalize_to_lf(e.new_text), e.replace_all) for e in edits]
    total = len(edits)
    for index, edit in enumerate(edits):
        if not edit.old_text:
            raise _empty(path, index, total)

    used_fuzzy = any(m is not None and m[2] for m in (_find(content, e.old_text) for e in edits))
    base = normalize_for_fuzzy_match(content) if used_fuzzy else content

    replacements: list[_Replacement] = []
    for index, edit in enumerate(edits):
        if edit.replace_all:
            spans = _find_every(base, edit.old_text)
            if not spans:
                raise _not_found(path, index, total)
            replacements.extend(_Replacement(index, at, length, edit.new_text) for at, length in spans)
            continue
        match = _find(base, edit.old_text)
        if match is None:
            raise _not_found(path, index, total)
        occurrences = _count_occurrences(base, edit.old_text)
        if occurrences > 1:
            raise _duplicate(path, index, total, occurrences)
        replacements.append(_Replacement(index, match[0], match[1], edit.new_text))

    replacements.sort(key=lambda r: r.index)
    for previous, current in zip(replacements, replacements[1:]):
        if previous.index + previous.length > current.index:
            raise EditError(
                f"edits[{previous.edit_index}] and edits[{current.edit_index}] overlap in {path}. Merge them into "
                "one edit or target disjoint regions."
            )

    new_content = (
        _apply_preserving_unchanged_lines(content, base, replacements) if used_fuzzy else _apply(base, replacements)
    )
    if new_content == content:
        raise _no_change(path, total)
    return new_content


def generate_unified_patch(path: str, old: str, new: str, context: int = 4) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=path, tofile=path, n=context
    )
    return "".join(lines)


def generate_diff_string(old: str, new: str, context: int = 4) -> tuple[str, Optional[int]]:
    """Pi's display diff: numbered ``+``/``-``/context lines and the first changed line of the new file."""
    old_lines, new_lines = old.split("\n"), new.split("\n")
    width = len(str(max(len(old_lines), len(new_lines))))
    parts: list[tuple[str, list[str]]] = []
    a, b = old.splitlines(), new.splitlines()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            parts.append((" ", a[i1:i2]))
            continue
        if i2 > i1:
            parts.append(("-", a[i1:i2]))
        if j2 > j1:
            parts.append(("+", b[j1:j2]))

    out: list[str] = []
    old_no = new_no = 1
    last_was_change = False
    first_changed: Optional[int] = None
    blank = " " * width
    for position, (kind, raw) in enumerate(parts):
        if kind != " ":
            if first_changed is None:
                first_changed = new_no
            for line in raw:
                if kind == "+":
                    out.append(f"+{str(new_no).rjust(width)} {line}")
                    new_no += 1
                else:
                    out.append(f"-{str(old_no).rjust(width)} {line}")
                    old_no += 1
            last_was_change = True
            continue

        trailing = position < len(parts) - 1 and parts[position + 1][0] != " "
        if last_was_change and trailing and len(raw) > context * 2:
            shown_head, skipped, shown_tail = raw[:context], len(raw) - 2 * context, raw[-context:]
        elif last_was_change and trailing:
            shown_head, skipped, shown_tail = raw, 0, []
        elif last_was_change:
            shown_head, skipped, shown_tail = raw[:context], max(0, len(raw) - context), []
        elif trailing:
            skipped = max(0, len(raw) - context)
            shown_head, shown_tail = [], raw[skipped:]
        else:
            shown_head, skipped, shown_tail = [], len(raw), []
            old_no += skipped
            new_no += skipped
            last_was_change = False
            continue
        for line in shown_head:
            out.append(f" {str(old_no).rjust(width)} {line}")
            old_no += 1
            new_no += 1
        if skipped:
            out.append(f" {blank} ...")
            old_no += skipped
            new_no += skipped
        for line in shown_tail:
            out.append(f" {str(old_no).rjust(width)} {line}")
            old_no += 1
            new_no += 1
        last_was_change = False
    return "\n".join(out), first_changed
