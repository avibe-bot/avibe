"""Matching and diff helpers for ``edit`` (C-7 section 4).

Ported from Pi ``packages/coding-agent/src/core/tools/edit-diff.ts`` (MIT,
Copyright (c) 2025 Mario Zechner): exact match first, then Pi's deterministic
normalized match with no similarity threshold, every edit matched against the
original, overlapping edits rejected. Avibe adds ``replaceAll`` (from Claude
Code and OpenCode): every occurrence in the tier that matched, each one a span
for the overlap check. Avibe departures from Pi: each edit matches in its own
tier, so one normalized edit no longer switches the whole batch to normalized
text; uniqueness is counted in the tier that matched; classic Mac CR line
endings are restored like CRLF.
"""

from __future__ import annotations

import bisect
import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional

BOM = "\ufeff"

# JavaScript's String.prototype.trimEnd set, which Pi uses (Python's isspace differs).
_TRAILING_SPACE = re.compile("[ \t\v\f\r\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+$")
_SMART_SINGLE = re.compile("[\u2018\u2019\u201a\u201b]")
_SMART_DOUBLE = re.compile("[\u201c\u201d\u201e\u201f]")
_DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
_SPECIAL_SPACES = re.compile("[\u00a0\u2002-\u200a\u202f\u205f\u3000]")


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
    """The style of the first line break. Pi knows CRLF and LF; Avibe also keeps classic Mac CR."""
    cr, lf = content.find("\r"), content.find("\n")
    if cr == -1 or (lf != -1 and lf < cr):
        return "\n"
    return "\r\n" if cr + 1 == lf else "\r"


def normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    return text if ending == "\n" else text.replace("\n", ending)


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


def _occurrences(content: str, normalized: Callable[[], str], old_text: str) -> tuple[bool, list[tuple[int, int]]]:
    """``(used_normalized, spans)``: the edit's occurrences in the first tier that has one.

    Exact spans are in the original text, normalized spans in the normalized text. Text that
    normalizes to nothing never matches in the normalized tier.
    """
    exact = _find_all(content, old_text)
    if exact:
        return False, [(index, len(old_text)) for index in exact]
    fuzzy_old = normalize_for_fuzzy_match(old_text)
    return True, [(index, len(fuzzy_old)) for index in _find_all(normalized(), fuzzy_old)]


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
    for replacement in sorted(replacements, key=lambda r: r.index, reverse=True):
        start = replacement.index - offset
        result = result[:start] + replacement.new_text + result[start + replacement.length :]
    return result


def _line_starts(text: str) -> list[int]:
    starts = [0]
    index = text.find("\n")
    while index != -1:
        starts.append(index + 1)
        index = text.find("\n", index + 1)
    return starts


def _overlap(path: str, first: int, second: int) -> EditError:
    return EditError(
        f"edits[{first}] and edits[{second}] overlap in {path}. Merge them into one edit or target disjoint regions."
    )


def _check_disjoint(path: str, replacements: list[_Replacement]) -> None:
    ordered = sorted(replacements, key=lambda r: r.index)
    for previous, current in zip(ordered, ordered[1:]):
        if previous.index + previous.length > current.index:
            raise _overlap(path, previous.edit_index, current.edit_index)


def _line_groups(content: str, normalized: str, replacements: list[_Replacement]) -> list[_Replacement]:
    """Normalized-tier replacements as whole-line replacements in original coordinates.

    Normalization keeps every line break, so line ``i`` of the normalized text is line ``i`` of the
    original. Replacements sharing lines form one group; its lines are rewritten from the
    normalized text with the group's replacements applied (Pi's way of applying a normalized match).
    """
    original_starts, normalized_starts = _line_starts(content), _line_starts(normalized)
    if len(original_starts) != len(normalized_starts):
        raise EditError("Cannot preserve unchanged lines because the base content has a different line count.")

    def line_of(index: int) -> int:
        return bisect.bisect_right(normalized_starts, index) - 1

    groups: list[list] = []  # [first_line, last_line, members]
    for replacement in sorted(replacements, key=lambda r: r.index):
        first, last = line_of(replacement.index), line_of(replacement.index + replacement.length - 1)
        if groups and first <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], last)
            groups[-1][2].append(replacement)
        else:
            groups.append([first, last, [replacement]])

    def bounds(starts: list[int], text: str, first: int, last: int) -> tuple[int, int]:
        return starts[first], starts[last + 1] if last + 1 < len(starts) else len(text)

    out: list[_Replacement] = []
    for first, last, members in groups:
        lo, hi = bounds(normalized_starts, normalized, first, last)
        original_lo, original_hi = bounds(original_starts, content, first, last)
        rewritten = _apply(normalized[lo:hi], members, lo)
        out.append(_Replacement(members[0].edit_index, original_lo, original_hi - original_lo, rewritten))
    return out


def apply_edits_to_normalized_content(content: str, edits: list[Edit], path: str) -> str:
    """Apply every edit to LF-normalized ``content`` and return the new content.

    Each edit matches in its own tier against the original. Exact edits replace exactly the text
    they matched; a normalized edit rewrites the whole lines it touches from the normalized text.
    Uniqueness, ``replaceAll``, overlap, and application are all per edit in original coordinates,
    so nothing an exact edit did not match is touched. (Pi matches a whole batch in normalized text
    as soon as one edit needs it.)
    """
    edits = [Edit(normalize_to_lf(e.old_text), normalize_to_lf(e.new_text), e.replace_all) for e in edits]
    total = len(edits)
    for index, edit in enumerate(edits):
        if not edit.old_text:
            raise _empty(path, index, total)

    cache: list[str] = []

    def normalized() -> str:
        if not cache:
            cache.append(normalize_for_fuzzy_match(content))
        return cache[0]

    exact: list[_Replacement] = []
    fuzzy: list[_Replacement] = []
    for index, edit in enumerate(edits):
        used_normalized, spans = _occurrences(content, normalized, edit.old_text)
        if not spans:
            raise _not_found(path, index, total)
        if not edit.replace_all and len(spans) > 1:
            raise _duplicate(path, index, total, len(spans))
        chosen = spans if edit.replace_all else spans[:1]
        (fuzzy if used_normalized else exact).extend(
            _Replacement(index, at, length, edit.new_text) for at, length in chosen
        )

    _check_disjoint(path, fuzzy)  # in normalized coordinates, before they are grouped into lines
    replacements = exact + (_line_groups(content, normalized(), fuzzy) if fuzzy else [])
    _check_disjoint(path, replacements)
    new_content = _apply(content, replacements)
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
